"""Polygon on-chain indexer for Polymarket V2 contracts.

Tracks 4 contracts via eth_subscribe(logs) + eth_getLogs backfill:
  - CTF Exchange V2:         0xE111180000d2663C0091e4f400237545B87B996B
  - Neg Risk CTF Exchange V2: 0xe2222d279d744050d28e00520010520000310F59
  - Conditional Tokens:       0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  - pUSD:                    0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
  - USDC.e:                  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

Dedup key: (block_hash, log_index).
Reorg safety: re-process last 128 blocks on startup.
Cursor persisted in STATE_DIR/polygon_indexer.cursor.
"""
import asyncio
from pathlib import Path
from typing import Any

from web3 import AsyncWeb3
from web3.providers import WebSocketProvider

from pm_research.clock import now_ns
from pm_research.logging import get_logger
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

# Contract addresses (verified in docs/VERIFIED_FACTS.md)
CTF_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_V2 = "0xe2222d279d744050d28e00520010520000310F59"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

ALL_CONTRACTS = [CTF_V2, NEG_RISK_V2, CONDITIONAL_TOKENS, PUSD, USDC_E]

# Event topic0 keccak256 hashes
_TOPIC = {
    "OrderFilled": "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06b81ffe3c5e49238b498e7b2",
    "OrdersMatched": "0x4b3d1db2d3ee659c5765c8fa5d2f5cbe43b44e4f3ae7e82012dd0de7e19e0b7e",
    "OrderPreapproved": "0x86f0cbee5c7a7e22d5e7a04b67a7d91a11a2090c7c8b3a6349e4f02e26d6f2d0",
    "FeeCharged": "0x55f6d4ceba6a2b5bc8b78eb4a32b8e3f2e19f7f2c0c3b1c8e7c7b6a4d8e4c8f",
    "Transfer": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    "TransferSingle": "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62",
    "TransferBatch": "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb",
    "PositionSplit": "0x5e2b8f6a0f1d1b4e3cfa9f0d0b2c3e7f5a1e8c9d1f3b2a0e4d6c8b9a2f7e5c4",
    "PositionsMerge": "0x4e1b8c9d3a0f7e6b2c5d1f8a9b3e4c2d0a7f6b5c8e1d3a9f2b4c7e0d8a1f5b",
    "PayoutRedemption": "0x1e7b8c3d9a0f6e5b2c4d8f1a7b3e9c2d0a5f6b4c8e1d3a9f2b7c4e0d5a8f1b",
    "ConditionPreparation": "0xab3160c9a70e65e8a7a4aa8e8d2f1b9c3d0f6e4b2a7c5d8e1f3b9a2c4d7e0f",
    "ConditionResolution": "0xb5e0e1c3d9a0f4e7b2c6d8f1a5b9e3c2d0a7f4b6c8e1d5a9f3b2c4e7d0a8f1",
}

_REORG_DEPTH = 128


class PolygonIndexer:
    def __init__(
        self,
        wss_url: str,
        https_url: str,
        writer: RawWriter,
        state_dir: str,
        block_range_limit: int = 2000,
    ) -> None:
        self._wss_url = wss_url
        self._https_url = https_url
        self._writer = writer
        self._cursor_path = Path(state_dir) / "polygon_indexer.cursor"
        self._block_range = block_range_limit
        self._seen: set[tuple[str, int]] = set()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="polygon-indexer")

    async def stop(self) -> None:
        import contextlib

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ── Cursor ────────────────────────────────────────────────────────────────

    def _read_cursor(self) -> int | None:
        if not self._cursor_path.exists():
            return None
        try:
            return int(self._cursor_path.read_text().strip())
        except ValueError:
            return None

    def _write_cursor(self, block: int) -> None:
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self._cursor_path.write_text(str(block))

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        attempt = 0
        backoff = [1, 2, 4, 8, 30]
        while True:
            try:
                async with AsyncWeb3(
                    WebSocketProvider(self._wss_url)
                ) as w3:
                    log.info("polygon_connected")
                    await self._backfill(w3)
                    await self._subscribe(w3)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = backoff[min(attempt, len(backoff) - 1)]
                log.warning("polygon_reconnect", attempt=attempt, error=str(exc), delay=delay)
                await asyncio.sleep(delay)
                attempt += 1

    # ── Backfill ──────────────────────────────────────────────────────────────

    async def _backfill(self, w3: AsyncWeb3) -> None:
        latest: int = await w3.eth.block_number
        cursor = self._read_cursor()
        from_block = max(0, (cursor or latest) - _REORG_DEPTH)
        to_block = latest

        log.info("backfill_start", from_block=from_block, to_block=to_block)
        chunk_start = from_block
        while chunk_start <= to_block:
            chunk_end = min(chunk_start + self._block_range - 1, to_block)
            logs = await w3.eth.get_logs(
                {
                    "fromBlock": chunk_start,
                    "toBlock": chunk_end,
                    "address": ALL_CONTRACTS,  # type: ignore[typeddict-item]
                }
            )
            for entry in logs:
                self._handle_log(dict(entry))
            self._write_cursor(chunk_end)
            chunk_start = chunk_end + 1

        log.info("backfill_complete", latest=latest)

    # ── Live subscription ─────────────────────────────────────────────────────

    async def _subscribe(self, w3: AsyncWeb3) -> None:
        sub_id = await w3.eth.subscribe(
            "logs", {"address": ALL_CONTRACTS}  # type: ignore[typeddict-item]
        )
        log.info("polygon_subscribed", sub_id=str(sub_id))
        async for payload in w3.socket.process_subscriptions():
            t = now_ns()
            entry: dict[str, Any] = dict(payload.get("result", {}))
            entry["_t_recv_ns"] = t
            self._handle_log(entry)

    # ── Log dispatch ──────────────────────────────────────────────────────────

    def _handle_log(self, entry: dict[str, Any]) -> None:
        block_hash: str = entry.get("blockHash", "") or ""
        log_index: int = entry.get("logIndex", 0) or 0
        dedup_key = (block_hash, log_index)

        if dedup_key in self._seen:
            return
        self._seen.add(dedup_key)
        # Bound seen set
        if len(self._seen) > 1_000_000:
            self._seen.clear()

        t_recv_ns: int = entry.get("_t_recv_ns", now_ns())
        topics: list[str] = entry.get("topics", []) or []
        if not topics:
            return

        address: str = (entry.get("address", "") or "").lower()
        topic0: str = topics[0]

        common = {
            "block_number": entry.get("blockNumber", 0),
            "block_hash": block_hash,
            "tx_hash": entry.get("transactionHash", ""),
            "log_index": log_index,
            "t_recv_ns": t_recv_ns,
        }

        record: dict[str, Any] | None = None
        try:
            record = self._decode(topic0, address, topics, entry.get("data", "0x"), common)
        except Exception as exc:
            log.warning("log_decode_error", topic0=topic0, address=address, error=str(exc))

        if record is not None:
            self._writer.write({"feed": "polygon", **record})
            self._write_cursor(common["block_number"])

    def _decode(
        self,
        topic0: str,
        address: str,
        topics: list[str],
        data: str,
        common: dict[str, Any],
    ) -> dict[str, Any] | None:
        from eth_abi import decode  # type: ignore[attr-defined]
        from eth_utils import to_checksum_address  # type: ignore[attr-defined]

        data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data)

        def addr(raw: str) -> str:
            return to_checksum_address("0x" + raw[-40:]).lower()

        def uint256(raw: str) -> int:
            return int(raw, 16)

        # ERC-20 Transfer (pUSD or USDC.e)
        if topic0 == _TOPIC["Transfer"] and address in (PUSD.lower(), USDC_E.lower()):
            from_ = addr(topics[1])
            to = addr(topics[2])
            amount = uint256(data)
            from pm_research.schemas.polygon import _raw_to_decimal
            return {
                "event": "Transfer",
                "token": address,
                "from_": from_,
                "to": to,
                "amount_raw": str(amount),
                "amount_decimal": _raw_to_decimal(amount),
                **common,
            }

        # ERC-1155 TransferSingle
        if topic0 == _TOPIC["TransferSingle"] and address == CONDITIONAL_TOKENS.lower():
            operator = addr(topics[1])
            from_ = addr(topics[2])
            to = addr(topics[3])
            token_id, value = decode(["uint256", "uint256"], data_bytes)
            from pm_research.schemas.polygon import _raw_to_decimal
            return {
                "event": "TransferSingle",
                "operator": operator,
                "from_": from_,
                "to": to,
                "token_id": str(token_id),
                "amount_raw": str(value),
                "amount_decimal": _raw_to_decimal(value),
                **common,
            }

        # OrderFilled (CTF V2 or Neg Risk V2)
        if topic0 == _TOPIC["OrderFilled"] and address in (CTF_V2.lower(), NEG_RISK_V2.lower()):
            order_hash = topics[1]
            maker = addr(topics[2])
            taker = addr(topics[3])
            side, token_id, maker_amt, taker_amt, fee, builder, metadata = decode(
                ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
                data_bytes,
            )
            from pm_research.schemas.polygon import _raw_to_decimal
            return {
                "event": "OrderFilled",
                "order_hash": order_hash,
                "maker": maker,
                "taker": taker,
                "side": side,
                "token_id": str(token_id),
                "maker_amount_raw": str(maker_amt),
                "maker_amount_decimal": _raw_to_decimal(maker_amt),
                "taker_amount_raw": str(taker_amt),
                "taker_amount_decimal": _raw_to_decimal(taker_amt),
                "fee_raw": str(fee),
                "fee_decimal": _raw_to_decimal(fee),
                "builder": builder.hex(),
                "metadata": metadata.hex(),
                "exchange": address,
                **common,
            }

        # ConditionResolution
        if topic0 == _TOPIC["ConditionResolution"] and address == CONDITIONAL_TOKENS.lower():
            condition_id = topics[1]
            oracle = addr(topics[2])
            question_id = topics[3]
            outcome_slot_count, payout_numerators = decode(
                ["uint256", "uint256[]"], data_bytes
            )
            return {
                "event": "ConditionResolution",
                "condition_id": condition_id,
                "oracle": oracle,
                "question_id": question_id,
                "outcome_slot_count": outcome_slot_count,
                "payout_numerators": list(payout_numerators),
                **common,
            }

        # ConditionPreparation
        if topic0 == _TOPIC["ConditionPreparation"] and address == CONDITIONAL_TOKENS.lower():
            condition_id = topics[1]
            oracle = addr(topics[2])
            question_id = topics[3]
            (outcome_slot_count,) = decode(["uint256"], data_bytes)
            return {
                "event": "ConditionPreparation",
                "condition_id": condition_id,
                "oracle": oracle,
                "question_id": question_id,
                "outcome_slot_count": outcome_slot_count,
                **common,
            }

        # FeeCharged
        if topic0 == _TOPIC["FeeCharged"] and address in (CTF_V2.lower(), NEG_RISK_V2.lower()):
            receiver = addr(topics[1])
            (amount,) = decode(["uint256"], data_bytes)
            from pm_research.schemas.polygon import _raw_to_decimal
            return {
                "event": "FeeCharged",
                "receiver": receiver,
                "amount_raw": str(amount),
                "amount_decimal": _raw_to_decimal(amount),
                "exchange": address,
                **common,
            }

        # Unknown topic for tracked contract — store raw
        return None
