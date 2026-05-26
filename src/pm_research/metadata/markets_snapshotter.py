"""Gamma API metadata snapshotter.

Polls GET /events?tag_id=102127&closed=false every N seconds.
Writes full event+market objects to pm_meta/ feed.
tag_id=102127 = "Up or Down" — universal discovery for all horizons.
"""
import asyncio
from typing import Any

import httpx

from pm_research.clock import now_ns
from pm_research.logging import get_logger
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_TAG_ID = 102127
_PAGE_SIZE = 500

_BACKOFF = [1, 2, 4, 8, 30]


async def _fetch_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> dict[str, Any] | list[Any]:
    last_exc: Exception | None = None
    for delay in [*_BACKOFF, None]:
        try:
            r = await client.get(url, params=params, timeout=15.0)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
        except Exception as exc:
            last_exc = exc
            log.warning("gamma_request_error", url=url, error=str(exc))
            if delay is not None:
                await asyncio.sleep(delay)
    raise RuntimeError(f"Gamma request exhausted retries: {last_exc}") from last_exc


async def _paginate_tag(client: httpx.AsyncClient, closed: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = await _fetch_with_retry(
            client,
            f"{_GAMMA_BASE}/events",
            {
                "tag_id": _TAG_ID,
                "closed": str(closed).lower(),
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
        )
        if not isinstance(data, list):
            break
        if not data:
            break
        results.extend(data)
        if len(data) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return results


class MarketsSnapshotter:
    def __init__(
        self,
        writer: RawWriter,
        interval_s: int = 300,
        allowed_assets: frozenset[str] | None = None,
    ) -> None:
        self._writer = writer
        self._interval = interval_s
        self._allowed = allowed_assets
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="markets-snapshotter")

    async def stop(self) -> None:
        import contextlib

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await self._snapshot(client)
                except Exception as exc:
                    log.error("snapshotter_error", error=str(exc))
                await asyncio.sleep(self._interval)

    async def _snapshot(self, client: httpx.AsyncClient) -> None:
        t = now_ns()
        events = await _paginate_tag(client, closed=False)
        count = 0
        for event in events:
            markets: list[dict[str, Any]] = event.get("markets", []) or []
            for market in markets:
                asset_ok = self._asset_allowed(market)
                if not asset_ok:
                    continue
                self._writer.write(
                    {
                        "feed": "pm_meta",
                        "t_recv_ns": t,
                        "event_type": "market_snapshot",
                        "event": event,
                        "market": market,
                    }
                )
                count += 1
        log.info("snapshot_complete", markets_written=count)

    def _asset_allowed(self, market: dict[str, Any]) -> bool:
        if self._allowed is None:
            return True
        slug: str = market.get("slug", "") or ""
        # Hourly slugs use full names ("bitcoin-…"); 5m/15m use short ("btc-…").
        _full = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "xrp": "xrp", "doge": "doge"}
        prefixes = set(self._allowed) | {_full[a] for a in self._allowed if a in _full}
        return any(slug.startswith(p) for p in prefixes)
