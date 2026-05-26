"""Heartbeat watchdog.

Reads journald every 60s. Verifies each service emitted a heartbeat within 90s.
Checks disk usage < 80%. Alerts Discord + Healthchecks on failure.
"""
import asyncio
import subprocess
from pathlib import Path

import httpx

from pm_research.logging import get_logger

log = get_logger(__name__)

_SERVICES = [
    "pm-clob-collector",
    "binance-collector",
    "polygon-indexer",
    "pm-metadata-snapshotter",
    "wallet-attribution",
    "pipeline-rotator",
]
_HEARTBEAT_MAX_AGE_S = 90
_DISK_MAX_PCT = 80


class HeartbeatWatchdog:
    def __init__(
        self,
        data_dir: str,
        discord_webhook_url: str,
        healthchecks_url: str,
        interval_s: int = 60,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._discord_url = discord_webhook_url
        self._hc_url = healthchecks_url
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="heartbeat-watchdog")

    async def stop(self) -> None:
        import contextlib

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                await asyncio.sleep(self._interval)
                await self._check(client)

    async def _check(self, client: httpx.AsyncClient) -> None:
        issues: list[str] = []

        # Check each service heartbeat via journald
        for svc in _SERVICES:
            age = self._last_heartbeat_age(svc)
            if age is None or age > _HEARTBEAT_MAX_AGE_S:
                issues.append(f"{svc}: no heartbeat for {age or 'unknown'}s")

        # Check disk usage
        disk_pct = self._disk_usage_pct()
        if disk_pct > _DISK_MAX_PCT:
            issues.append(f"disk usage {disk_pct:.0f}% > {_DISK_MAX_PCT}%")

        if not issues:
            await self._ping_hc(client)
            return

        msg = "Watchdog alerts:\n" + "\n".join(f"• {i}" for i in issues)
        log.error("watchdog_alert", issues=issues)
        await self._alert(client, msg)

    def _last_heartbeat_age(self, service: str) -> float | None:
        try:
            result = subprocess.run(
                [  # noqa: S603, S607
                    "journalctl",
                    "-u", service,
                    "--since", f"-{_HEARTBEAT_MAX_AGE_S}s",
                    "--grep", "heartbeat",
                    "-q", "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.stdout.strip():
                return 0.0  # Found recent heartbeat
            return float(_HEARTBEAT_MAX_AGE_S + 1)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def _disk_usage_pct(self) -> float:
        try:
            import shutil

            usage = shutil.disk_usage(self._data_dir)
            return usage.used / usage.total * 100
        except Exception:
            return 0.0

    async def _ping_hc(self, client: httpx.AsyncClient) -> None:
        if not self._hc_url:
            return
        try:
            await client.get(self._hc_url)
        except Exception as exc:
            log.warning("hc_ping_failed", error=str(exc))

    async def _alert(self, client: httpx.AsyncClient, msg: str) -> None:
        if self._discord_url:
            try:
                await client.post(self._discord_url, json={"content": msg})
            except Exception as exc:
                log.error("discord_alert_failed", error=str(exc))
        if self._hc_url:
            try:
                await client.get(f"{self._hc_url}/fail")
            except Exception as exc:
                log.error("hc_fail_ping_error", error=str(exc))
