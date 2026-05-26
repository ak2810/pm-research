"""Polymarket CLOB WebSocket collector.

- Discovers markets via Gamma API tag 102127 every 30s.
- Also picks up new_market events from WS (custom_feature_enabled=true).
- Subscribes to all active token_ids via market channel.
- Handles all 7 server message types.
- Reconnects on disconnect; fetches fresh book snapshot on reconnect.
- active:false new_market events are NOT auto-subscribed (verified fact).
"""
import asyncio
import datetime
import json
from typing import Any

import httpx
import websockets

from pm_research.clock import now_ns
from pm_research.logging import get_logger
from pm_research.schemas.polymarket import NewMarketMsg, parse_ws_frame
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CLOB_BASE = "https://clob.polymarket.com"
_TAG_ID = 102127
_BACKOFF = [1, 2, 4, 8, 30]

# Hourly market slugs use full asset names; 5m/15m use abbreviations.
# Map short→full so both forms are recognised by the same allowed_assets set.
_SHORT_TO_FULL: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
    "doge": "doge",
}


class PolymarketClobCollector:
    def __init__(
        self,
        writer: RawWriter,
        allowed_assets: frozenset[str],
        discovery_interval_s: int = 30,
        market_max_age_hours: int = 6,
    ) -> None:
        self._writer = writer
        self._allowed = allowed_assets
        self._discovery_interval = discovery_interval_s
        self._max_age_hours = market_max_age_hours

        # token_id → condition_id mapping for active subscriptions
        self._subscribed: dict[str, str] = {}
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._tasks.append(
            asyncio.create_task(self._discovery_loop(), name="pm-discovery")
        )
        self._tasks.append(
            asyncio.create_task(self._collect_loop(), name="pm-collector")
        )

    async def stop(self) -> None:
        import contextlib

        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self._discover()
            except Exception as exc:
                log.error("discovery_error", error=str(exc))
            await asyncio.sleep(self._discovery_interval)

    async def _discover(self) -> None:
        async with httpx.AsyncClient() as client:
            offset = 0
            while True:
                r = await client.get(
                    f"{_GAMMA_BASE}/events",
                    params={
                        "tag_id": _TAG_ID,
                        "closed": "false",
                        "limit": 500,
                        "offset": offset,
                    },
                    timeout=15.0,
                )
                r.raise_for_status()
                events: list[dict[str, Any]] = r.json()
                if not events:
                    break
                for event in events:
                    for market in event.get("markets", []) or []:
                        self._consider_market(market)
                if len(events) < 500:
                    break
                offset += 500

    def _consider_market(self, market: dict[str, Any]) -> None:
        if not market.get("acceptingOrders"):
            return
        if market.get("negRisk"):
            return

        # Filter by endDate: must be in the future and within configured window
        end_date_str: str = market.get("endDate", "") or ""
        if end_date_str:
            try:
                end_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                if end_dt <= now_utc:
                    return  # already expired
                if end_dt > now_utc + datetime.timedelta(hours=self._max_age_hours):
                    return  # too far in future, not yet active
            except ValueError:
                pass

        slug: str = market.get("slug", "") or ""
        # Match both short prefixes (5m/15m: "btc-updown-…") and full-name
        # prefixes (hourly: "bitcoin-up-or-down-…").
        allowed_prefixes = set(self._allowed) | {
            _SHORT_TO_FULL[a] for a in self._allowed if a in _SHORT_TO_FULL
        }
        if not any(slug.startswith(p) for p in allowed_prefixes):
            return

        raw_ids: str | list[str] = market.get("clobTokenIds", "[]") or "[]"
        if isinstance(raw_ids, str):
            token_ids: list[str] = json.loads(raw_ids)
        else:
            token_ids = raw_ids

        condition_id: str = market.get("conditionId", "") or ""
        for tid in token_ids:
            if tid not in self._subscribed:
                self._subscribed[tid] = condition_id
                log.info("market_discovered", token_id=tid[:20], slug=slug)

    # ── Collector ─────────────────────────────────────────────────────────────

    async def _collect_loop(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                log.warning("pm_reconnect", attempt=attempt, error=str(exc), delay=delay)
                attempt += 1
                # Write disconnect event
                self._writer.write(
                    {
                        "feed": "pm_clob",
                        "t_recv_ns": now_ns(),
                        "event_type": "disconnect",
                        "reason": str(exc),
                        "attempt": attempt,
                    }
                )
                await asyncio.sleep(delay)

    async def _connect_session(self) -> None:
        # Wait for at least one subscription before connecting
        while not self._subscribed:
            await asyncio.sleep(1.0)

        log.info("pm_connecting", token_count=len(self._subscribed))
        async with websockets.connect(_WS_URL, ping_interval=60) as ws:
            token_ids = list(self._subscribed)
            sub = {
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(sub))
            self._writer.write(
                {
                    "feed": "pm_clob",
                    "t_recv_ns": now_ns(),
                    "event_type": "subscribe_ack",
                    "asset_ids": token_ids,
                }
            )
            log.info("pm_subscribed", count=len(token_ids))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=90.0)
                except TimeoutError:
                    log.warning("pm_recv_timeout")
                    break

                t = now_ns()
                try:
                    payload: Any = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("pm_json_error", error=str(exc))
                    continue

                # Server sends single object OR array
                items: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
                for item in items:
                    self._handle_frame(item, t)

    def _handle_frame(self, raw: dict[str, Any], t_recv_ns: int) -> None:
        try:
            msg = parse_ws_frame(raw)
        except (ValueError, Exception) as exc:
            log.warning("pm_parse_error", error=str(exc), keys=list(raw.keys()))
            return

        # Auto-discover new markets from WS (active=False → queue for Gamma poll)
        if isinstance(msg, NewMarketMsg) and msg.active:
            for tid in msg.clob_token_ids:
                if tid not in self._subscribed:
                    self._subscribed[tid] = msg.condition_id
                    log.info("ws_new_market_subscribed", token_id=tid[:20], slug=msg.slug)

        self._writer.write(
            {"feed": "pm_clob", "t_recv_ns": t_recv_ns, **raw}
        )
