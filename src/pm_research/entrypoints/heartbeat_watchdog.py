import asyncio

from pm_research.clock import check_ntp_drift
from pm_research.config import get_settings
from pm_research.heartbeat_watchdog import HeartbeatWatchdog
from pm_research.logging import configure_logging, get_logger

log = get_logger(__name__)


async def main() -> None:
    cfg = get_settings()
    configure_logging()
    check_ntp_drift(cfg.max_ntp_drift_ms)

    watchdog = HeartbeatWatchdog(
        data_dir=cfg.data_dir,
        discord_webhook_url=cfg.discord_webhook_url,
        healthchecks_url=cfg.healthchecks_url,
    )

    await watchdog.start()

    try:
        await asyncio.Event().wait()
    finally:
        await watchdog.stop()


if __name__ == "__main__":
    asyncio.run(main())
