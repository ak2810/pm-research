import asyncio

from pm_research.clock import check_ntp_drift
from pm_research.collectors.polymarket_clob import PolymarketClobCollector
from pm_research.config import get_settings
from pm_research.heartbeat import Heartbeat
from pm_research.logging import configure_logging, get_logger
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)


async def main() -> None:
    cfg = get_settings()
    configure_logging()
    check_ntp_drift(cfg.max_ntp_drift_ms)

    writer = RawWriter("pm_clob", cfg.data_dir)
    heartbeat = Heartbeat(
        "pm-clob-collector",
        cfg.healthchecks_url,
        cfg.discord_webhook_url,
    )
    collector = PolymarketClobCollector(
        writer=writer,
        allowed_assets=frozenset(["btc", "eth", "xrp", "sol", "doge"]),
        discovery_interval_s=cfg.gamma_discovery_interval_s,
        market_max_age_hours=cfg.market_max_age_hours,
    )

    await heartbeat.start()
    await collector.start()

    try:
        await asyncio.Event().wait()
    finally:
        await collector.stop()
        await heartbeat.stop()
        writer.close()


if __name__ == "__main__":
    asyncio.run(main())
