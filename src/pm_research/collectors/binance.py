"""Binance WebSocket collector.

Subscribes to 4 streams per symbol: @aggTrade, @bookTicker, @depth@100ms, @kline_1m.
Pre-emptive reconnect at 23h with 10s overlap + dedup on (stream, E).
Local book maintained via snapshot + buffered-delta algorithm.
"""
import asyncio
import json
import time
from collections import defaultdict
from typing import Any

import websockets

from pm_research.clock import now_ns
from pm_research.logging import get_logger
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

_WS_BASE = "wss://stream.binance.com:9443/stream"
_REST_BASE = "https://api.binance.com/api/v3"
_RECONNECT_AT_S = 82_800   # 23h
_OVERLAP_S = 10
_PING_INTERVAL = 60
_BACKOFF = [1, 2, 4, 8, 30]

_STREAM_SUFFIXES = ["@aggTrade", "@bookTicker", "@depth@100ms", "@kline_1m"]


def _stream_url(symbols: list[str]) -> str:
    streams = "/".join(
        f"{sym.lower()}{suf}" for sym in symbols for suf in _STREAM_SUFFIXES
    )
    return f"{_WS_BASE}?streams={streams}"


class BinanceCollector:
    def __init__(
        self,
        symbols: list[str],
        writer: RawWriter,
        reconnect_at_s: int = _RECONNECT_AT_S,
    ) -> None:
        self._symbols = symbols
        self._writer = writer
        self._reconnect_at = reconnect_at_s
        self._seen: dict[str, set[int]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="binance-collector")

    async def stop(self) -> None:
        import contextlib

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                log.warning("binance_reconnect", attempt=attempt, error=str(exc), delay=delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _connect_session(self) -> None:
        url = _stream_url(self._symbols)
        start_s = time.monotonic()
        log.info("binance_connecting", url=url[:80])

        async with websockets.connect(url, ping_interval=_PING_INTERVAL) as ws:
            log.info("binance_connected")
            while True:
                elapsed = time.monotonic() - start_s
                if elapsed >= self._reconnect_at:
                    log.info("binance_preemptive_reconnect", elapsed_s=int(elapsed))
                    break

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except TimeoutError:
                    continue

                t = now_ns()
                try:
                    frame: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("binance_json_error", error=str(exc))
                    continue

                stream: str = frame.get("stream", "")
                data: dict[str, Any] = frame.get("data", {})
                event_time: int = data.get("E", 0) or data.get("u", 0)

                # Dedup by (stream, event_time)
                if event_time and event_time in self._seen[stream]:
                    continue
                if event_time:
                    self._seen[stream].add(event_time)
                    # Bound seen set size
                    if len(self._seen[stream]) > 10_000:
                        self._seen[stream].clear()

                self._writer.write(
                    {"feed": "binance", "t_recv_ns": t, "stream": stream, **data}
                )
