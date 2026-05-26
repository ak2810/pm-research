import asyncio

import httpx

from pm_research.logging import get_logger

log = get_logger(__name__)


class Heartbeat:
    def __init__(
        self,
        service_name: str,
        healthchecks_url: str,
        discord_webhook_url: str,
        interval_s: int = 60,
    ) -> None:
        self._service = service_name
        self._hc_url = healthchecks_url
        self._discord_url = discord_webhook_url
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat-{self._service}")

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
                await self._ping(client)

    async def _ping(self, client: httpx.AsyncClient) -> None:
        log.info("heartbeat_ping", service=self._service)
        if not self._hc_url:
            return
        try:
            r = await client.get(self._hc_url)
            r.raise_for_status()
        except Exception as exc:
            log.error("heartbeat_failed", service=self._service, error=str(exc))
            await self._alert(str(exc))

    async def _alert(self, error: str) -> None:
        if not self._discord_url:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(
                    self._discord_url,
                    json={"content": f":red_circle: `{self._service}` heartbeat failed: {error}"},
                )
            except Exception as exc:
                log.error("discord_alert_failed", error=str(exc))
