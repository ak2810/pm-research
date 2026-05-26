"""Polymarket CLOB WebSocket collector.

- Discovers markets via slug-based probing (5m, 15m, hourly windows).
- Also picks up new_market events from WS (custom_feature_enabled=true).
- Subscribes to all active token_ids via market channel.
- Handles all 7 server message types.
- Reconnects every ~4.5min to rotate subscriptions as 5m/15m windows roll.
- active:false new_market events are NOT auto-subscribed (verified fact).
"""
import asyncio
import datetime
import json
import time
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
_BACKOFF = [1, 2, 4, 8, 30]

# short → full name for hourly market slugs
_SHORT_TO_HOURLY: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
    "doge": "dogecoin",
}

# How long before each window boundary to reconnect (seconds)
_RECONNECT_BEFORE_S = 30

# Reconnect period for 5m markets (reconnect every ~4.5 min to rotate)
_WS_SESSION_MAX_S = 270


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

        # token_id → (condition_id, end_dt)
        self._subscribed: dict[str, tuple[str, datetime.datetime]] = {}
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
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Purge tokens whose market has already ended
        expired = [tid for tid, (_, end_dt) in self._subscribed.items() if end_dt <= now_utc]
        for tid in expired:
            del self._subscribed[tid]

        slugs = self._candidate_slugs(now_utc)
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *[self._fetch_slug(client, s) for s in slugs],
                return_exceptions=True,
            )
        for res in results:
            if not isinstance(res, list):
                continue
            for event in res:
                for market in event.get("markets", []) or []:
                    self._consider_market(market, now_utc)

    def _candidate_slugs(self, now: datetime.datetime) -> list[str]:
        """Generate slug candidates for current and upcoming 5m/15m/hourly windows."""
        slugs: list[str] = []
        ts = int(now.timestamp())

        for short in self._allowed:
            # 5m: current window + next 3 (cover ~20 min of look-ahead)
            base5 = (ts // 300) * 300
            for i in range(4):
                slugs.append(f"{short}-updown-5m-{base5 + i * 300}")

            # 15m: current window + next 2
            base15 = (ts // 900) * 900
            for i in range(3):
                slugs.append(f"{short}-updown-15m-{base15 + i * 900}")

            # Hourly: current hour + next 2
            full = _SHORT_TO_HOURLY.get(short, short)
            hour_base = now.replace(minute=0, second=0, microsecond=0)
            for i in range(3):
                slugs.append(self._hourly_slug(full, hour_base + datetime.timedelta(hours=i)))

        return slugs

    @staticmethod
    def _hourly_slug(asset_full: str, utc_dt: datetime.datetime) -> str:
        # ET = UTC-4 (EDT, valid ~Mar-Nov); acceptable approximation for slug generation
        et_dt = utc_dt - datetime.timedelta(hours=4)
        month = et_dt.strftime("%b").lower()
        day = et_dt.day
        year = et_dt.year
        h = et_dt.hour
        if h == 0:
            ampm = "12am"
        elif h < 12:
            ampm = f"{h}am"
        elif h == 12:
            ampm = "12pm"
        else:
            ampm = f"{h - 12}pm"
        return f"{asset_full}-up-or-down-{month}-{day}-{year}-{ampm}-et"

    async def _fetch_slug(self, client: httpx.AsyncClient, slug: str) -> list[dict[str, Any]]:
        try:
            r = await client.get(
                f"{_GAMMA_BASE}/events",
                params={"slug": slug, "limit": 1},
                timeout=10.0,
            )
            if r.status_code == 200:
                return r.json()  # type: ignore[no-any-return]
        except Exception:
            pass
        return []

    def _consider_market(self, market: dict[str, Any], now_utc: datetime.datetime) -> None:
        if not market.get("acceptingOrders"):
            return
        if market.get("negRisk"):
            return

        end_date_str: str = market.get("endDate", "") or ""
        end_dt: datetime.datetime | None = None
        if end_date_str:
            try:
                end_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if not end_dt.tzinfo:
                    end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
                if end_dt <= now_utc:
                    return
                if end_dt > now_utc + datetime.timedelta(hours=self._max_age_hours):
                    return
            except (ValueError, TypeError):
                return  # reject markets with unparseable endDate

        slug: str = market.get("slug", "") or ""
        raw_ids: str | list[str] = market.get("clobTokenIds", "[]") or "[]"
        if isinstance(raw_ids, str):
            token_ids: list[str] = json.loads(raw_ids)
        else:
            token_ids = raw_ids

        condition_id: str = market.get("conditionId", "") or ""
        effective_end_dt = end_dt if end_dt is not None else now_utc + datetime.timedelta(hours=self._max_age_hours)
        for tid in token_ids:
            if tid not in self._subscribed:
                self._subscribed[tid] = (condition_id, effective_end_dt)
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
        # Wait for at least one active subscription
        while not self._subscribed:
            await asyncio.sleep(1.0)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        token_ids = [tid for tid, (_, end_dt) in self._subscribed.items() if end_dt > now_utc]
        if not token_ids:
            await asyncio.sleep(5.0)
            return

        log.info("pm_connecting", token_count=len(token_ids))
        deadline = time.monotonic() + _WS_SESSION_MAX_S

        async with websockets.connect(_WS_URL, ping_interval=60) as ws:
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
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.info("pm_session_rotate")
                    break  # reconnect to refresh subscriptions

                timeout = min(remaining, 30.0)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except TimeoutError:
                    continue  # check deadline, loop

                t = now_ns()
                try:
                    payload: Any = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("pm_json_error", error=str(exc))
                    continue

                items: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
                for item in items:
                    self._handle_frame(item, t)

    def _handle_frame(self, raw: dict[str, Any], t_recv_ns: int) -> None:
        self._writer.write({"feed": "pm_clob", "t_recv_ns": t_recv_ns, **raw})

        try:
            msg = parse_ws_frame(raw)
        except (ValueError, Exception) as exc:
            log.warning("pm_parse_error", error=str(exc), keys=list(raw.keys()))
            return

        if isinstance(msg, NewMarketMsg) and msg.active:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            ws_end_dt = now_utc + datetime.timedelta(hours=self._max_age_hours)
            for tid in msg.clob_token_ids:
                if tid not in self._subscribed:
                    self._subscribed[tid] = (msg.condition_id, ws_end_dt)
                    log.info("ws_new_market_subscribed", token_id=tid[:20], slug=msg.slug)
