import asyncio

from pm_research.clock import check_ntp_drift
from pm_research.config import get_settings
from pm_research.heartbeat import Heartbeat
from pm_research.logging import configure_logging, get_logger
from pm_research.pipeline.rotator import PipelineRotator

log = get_logger(__name__)


async def main() -> None:
    cfg = get_settings()
    configure_logging()
    check_ntp_drift(cfg.max_ntp_drift_ms)

    heartbeat = Heartbeat(
        "pipeline-rotator",
        cfg.healthchecks_url,
        cfg.discord_webhook_url,
    )
    rotator = PipelineRotator(
        data_dir=cfg.data_dir,
        s3_bucket=cfg.s3_bucket,
        s3_region=cfg.aws_default_region,
        interval_s=300,
    )

    await heartbeat.start()
    await rotator.start()

    try:
        await asyncio.Event().wait()
    finally:
        await rotator.stop()
        await heartbeat.stop()


if __name__ == "__main__":
    asyncio.run(main())
