import asyncio

from pm_research.clock import check_ntp_drift
from pm_research.collectors.binance import BinanceCollector
from pm_research.config import get_settings
from pm_research.heartbeat import Heartbeat
from pm_research.logging import configure_logging, get_logger
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

_SYMBOLS = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT"]


async def main() -> None:
    cfg = get_settings()
    configure_logging()
    check_ntp_drift(cfg.max_ntp_drift_ms)

    writer = RawWriter("binance", cfg.data_dir)
    heartbeat = Heartbeat(
        "binance-collector",
        cfg.healthchecks_url,
        cfg.discord_webhook_url,
    )
    collector = BinanceCollector(
        symbols=_SYMBOLS,
        writer=writer,
        reconnect_at_s=cfg.binance_reconnect_at_s,
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
