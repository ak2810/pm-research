import asyncio

from pm_research.clock import check_ntp_drift
from pm_research.collectors.polygon_indexer import PolygonIndexer
from pm_research.config import get_settings
from pm_research.heartbeat import Heartbeat
from pm_research.logging import configure_logging, get_logger
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)


async def main() -> None:
    cfg = get_settings()
    configure_logging()
    check_ntp_drift(cfg.max_ntp_drift_ms)

    writer = RawWriter("polygon", cfg.data_dir)
    heartbeat = Heartbeat(
        "polygon-indexer",
        cfg.healthchecks_url,
        cfg.discord_webhook_url,
    )
    indexer = PolygonIndexer(
        wss_url=cfg.polygon_wss_url,
        https_url=cfg.polygon_https_url,
        writer=writer,
        state_dir=cfg.state_dir,
        block_range_limit=cfg.alchemy_block_range_limit,
    )

    await heartbeat.start()
    await indexer.start()

    try:
        await asyncio.Event().wait()
    finally:
        await indexer.stop()
        await heartbeat.stop()
        writer.close()


if __name__ == "__main__":
    asyncio.run(main())
