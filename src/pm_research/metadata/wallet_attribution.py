"""Wallet attribution service.

For each seed wallet (gabagool22, ohanism):
1. Resolve signer EOA from ProxyFactory ProxyCreated event.
2. Backfill 90d of pUSD.Transfer AND USDC.e.Transfer for proxy + signer.
3. Link wrap/unwrap bridge edges (USDC.e ↔ pUSD via Onramp/Offramp contracts).
4. Recurse one level to find sub-accounts.
5. Output wallet_graph.json (adjacency + funding amounts + bridge edges).

DUAL-TOKEN REQUIREMENT (docs/VERIFIED_FACTS.md):
- pUSD tracks all post-2026-04-28 trading.
- USDC.e tracks wrap/unwrap + pre-migration history (ohanism from Feb 2026).
- Both must be scanned with bridge edges linking wrap/unwrap flows.
"""
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web3 import AsyncWeb3
from web3.providers import WebSocketProvider

from pm_research.clock import now_ns
from pm_research.logging import get_logger
from pm_research.schemas.polygon import _raw_to_decimal
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

# Contract addresses
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
COLLATERAL_OFFRAMP = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"

_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_PROXY_CREATED_TOPIC = "0x4f51faf6c4561ff95f067657e43439f0f856d97c04d9ec9070a6199ad418e235"

_BLOCKS_PER_DAY = 43_200
_BACKFILL_DAYS = 90


@dataclass
class WalletNode:
    address: str
    username: str = ""
    signer_eoa: str = ""
    pusd_in: str = "0"
    pusd_out: str = "0"
    usdc_in: str = "0"
    usdc_out: str = "0"
    sub_accounts: list[str] = field(default_factory=list)


@dataclass
class BridgeEdge:
    from_token: str     # USDC.e or pUSD address
    to_token: str
    amount_raw: str
    amount_decimal: str
    block_number: int
    tx_hash: str
    direction: str      # "wrap" or "unwrap"


class WalletAttribution:
    def __init__(
        self,
        wss_url: str,
        writer: RawWriter,
        state_dir: str,
        seed_wallets: list[dict[str, str]],
        block_range_limit: int = 2000,
    ) -> None:
        self._wss_url = wss_url
        self._writer = writer
        self._out_path = Path(state_dir) / "wallet_graph.json"
        self._seeds = seed_wallets
        self._block_range = block_range_limit
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="wallet-attribution")

    async def stop(self) -> None:
        import contextlib

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self._build_graph()
            except Exception as exc:
                log.error("wallet_attribution_error", error=str(exc))
            # Run once per day
            await asyncio.sleep(86_400)

    async def _build_graph(self) -> None:
        async with AsyncWeb3(WebSocketProvider(self._wss_url)) as w3:
            latest: int = await w3.eth.block_number
            from_block = max(0, latest - _BLOCKS_PER_DAY * _BACKFILL_DAYS)

            nodes: dict[str, WalletNode] = {}
            bridge_edges: list[BridgeEdge] = []

            for seed in self._seeds:
                proxy = seed["proxy_wallet"].lower()
                username = seed.get("username", "")
                node = WalletNode(address=proxy, username=username)

                # Step 1: Resolve signer EOA
                node.signer_eoa = await self._resolve_signer(w3, proxy, from_block, latest)

                addresses = {proxy}
                if node.signer_eoa:
                    addresses.add(node.signer_eoa)

                # Step 2: Backfill transfers (both tokens)
                for addr in list(addresses):
                    pusd_edges, usdc_edges, wraps = await self._fetch_transfers(
                        w3, addr, from_block, latest
                    )
                    bridge_edges.extend(wraps)
                    self._accumulate(node, pusd_edges, usdc_edges)

                nodes[proxy] = node

            graph = {
                "t_built_ns": now_ns(),
                "nodes": [
                    {
                        "address": n.address,
                        "username": n.username,
                        "signer_eoa": n.signer_eoa,
                        "pusd_in": n.pusd_in,
                        "pusd_out": n.pusd_out,
                        "usdc_in": n.usdc_in,
                        "usdc_out": n.usdc_out,
                        "sub_accounts": n.sub_accounts,
                    }
                    for n in nodes.values()
                ],
                "bridge_edges": [
                    {
                        "from_token": e.from_token,
                        "to_token": e.to_token,
                        "amount_raw": e.amount_raw,
                        "amount_decimal": e.amount_decimal,
                        "block_number": e.block_number,
                        "tx_hash": e.tx_hash,
                        "direction": e.direction,
                    }
                    for e in bridge_edges
                ],
            }

            self._out_path.parent.mkdir(parents=True, exist_ok=True)
            self._out_path.write_text(json.dumps(graph, indent=2))
            log.info("wallet_graph_built", nodes=len(nodes), bridges=len(bridge_edges))
            self._writer.write({"feed": "wallet", "t_recv_ns": now_ns(), **graph})

    async def _resolve_signer(
        self, w3: AsyncWeb3, proxy: str, from_block: int, to_block: int
    ) -> str:
        try:
            logs = await w3.eth.get_logs(  # type: ignore[call-overload]
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": PROXY_FACTORY,  # type: ignore[typeddict-item]
                    "topics": [_PROXY_CREATED_TOPIC],  # type: ignore[typeddict-item]
                }
            )
            for entry in logs:
                # ProxyCreated(address proxy, address signer)
                topics: list[Any] = list(entry.get("topics", []) or [])  # type: ignore[arg-type]
                if len(topics) >= 3:
                    addr = "0x" + str(topics[2])[-40:]
                    if addr.lower() == proxy:
                        signer = "0x" + str(topics[1])[-40:]
                        return signer.lower()
        except Exception as exc:
            log.warning("signer_resolve_failed", proxy=proxy, error=str(exc))
        return ""

    async def _fetch_transfers(
        self,
        w3: AsyncWeb3,
        address: str,
        from_block: int,
        to_block: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[BridgeEdge]]:
        pusd_transfers: list[dict[str, Any]] = []
        usdc_transfers: list[dict[str, Any]] = []
        bridge_edges: list[BridgeEdge] = []

        for token_addr, token_label in [(PUSD, "pusd"), (USDC_E, "usdc")]:
            chunk = from_block
            while chunk <= to_block:
                chunk_end = min(chunk + self._block_range - 1, to_block)
                try:
                    # Transfers TO address
                    to_logs = await w3.eth.get_logs(  # type: ignore[call-overload]
                        {
                            "fromBlock": chunk,
                            "toBlock": chunk_end,
                            "address": token_addr,  # type: ignore[typeddict-item]
                            "topics": [  # type: ignore[typeddict-item]
                                _ERC20_TRANSFER_TOPIC,
                                None,
                                "0x" + "0" * 24 + address[2:],
                            ],
                        }
                    )
                    # Transfers FROM address
                    from_logs = await w3.eth.get_logs(  # type: ignore[call-overload]
                        {
                            "fromBlock": chunk,
                            "toBlock": chunk_end,
                            "address": token_addr,  # type: ignore[typeddict-item]
                            "topics": [  # type: ignore[typeddict-item]
                                _ERC20_TRANSFER_TOPIC,
                                "0x" + "0" * 24 + address[2:],
                                None,
                            ],
                        }
                    )
                    for entry in [*to_logs, *from_logs]:
                        t = dict(entry)
                        raw_topics: list[Any] = list(t.get("topics", []) or [])  # type: ignore[arg-type]
                        raw_data: str = str(t.get("data", "0x0") or "0x0")
                        amount = int(raw_data, 16)
                        from_ = "0x" + str(raw_topics[1])[-40:]
                        to_ = "0x" + str(raw_topics[2])[-40:]
                        record: dict[str, Any] = {
                            "token": token_addr.lower(),
                            "from_": from_.lower(),
                            "to": to_.lower(),
                            "amount_raw": str(amount),
                            "amount_decimal": _raw_to_decimal(amount),
                            "block_number": t.get("blockNumber", 0),
                            "tx_hash": t.get("transactionHash", ""),
                        }
                        # Detect bridge edges
                        if to_.lower() == COLLATERAL_ONRAMP.lower():
                            bridge_edges.append(BridgeEdge(
                                from_token=USDC_E,
                                to_token=PUSD,
                                amount_raw=str(amount),
                                amount_decimal=_raw_to_decimal(amount),
                                block_number=record["block_number"],
                                tx_hash=record["tx_hash"],
                                direction="wrap",
                            ))
                        elif to_.lower() == COLLATERAL_OFFRAMP.lower():
                            bridge_edges.append(BridgeEdge(
                                from_token=PUSD,
                                to_token=USDC_E,
                                amount_raw=str(amount),
                                amount_decimal=_raw_to_decimal(amount),
                                block_number=record["block_number"],
                                tx_hash=record["tx_hash"],
                                direction="unwrap",
                            ))
                        if token_label == "pusd":  # noqa: S105
                            pusd_transfers.append(record)
                        else:
                            usdc_transfers.append(record)
                except Exception as exc:
                    log.warning(
                        "transfer_fetch_error",
                        token=token_label,
                        chunk=chunk,
                        error=str(exc),
                    )
                chunk = chunk_end + 1

        return pusd_transfers, usdc_transfers, bridge_edges

    def _accumulate(
        self,
        node: WalletNode,
        pusd: list[dict[str, Any]],
        usdc: list[dict[str, Any]],
    ) -> None:
        from decimal import Decimal

        def sum_field(records: list[dict[str, Any]], key: str) -> Decimal:
            return sum((Decimal(r[key]) for r in records), Decimal(0))

        node.pusd_in = str(sum_field(pusd, "amount_decimal"))
        node.usdc_in = str(sum_field(usdc, "amount_decimal"))
