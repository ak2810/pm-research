import asyncio
from pathlib import Path

import yaml

from pm_research.clock import check_ntp_drift
from pm_research.config import get_settings
from pm_research.heartbeat import Heartbeat
from pm_research.logging import configure_logging, get_logger
from pm_research.metadata.wallet_attribution import WalletAttribution
from pm_research.storage.raw_writer import RawWriter

log = get_logger(__name__)

_SEED_WALLETS_PATH = "config/seed_wallets.yaml"


async def main() -> None:
    cfg = get_settings()
    configure_logging()
    check_ntp_drift(cfg.max_ntp_drift_ms)

    with Path(_SEED_WALLETS_PATH).open() as f:
        seed_config = yaml.safe_load(f)
    seed_wallets: list[dict[str, str]] = seed_config.get("wallets", [])

    writer = RawWriter("wallet", cfg.data_dir)
    heartbeat = Heartbeat(
        "wallet-attribution",
        cfg.healthchecks_url,
        cfg.discord_webhook_url,
    )
    attribution = WalletAttribution(
        wss_url=cfg.polygon_wss_url,
        writer=writer,
        state_dir=cfg.state_dir,
        seed_wallets=seed_wallets,
        block_range_limit=cfg.alchemy_block_range_limit,
    )

    await heartbeat.start()
    await attribution.start()

    try:
        await asyncio.Event().wait()
    finally:
        await attribution.stop()
        await heartbeat.stop()
        writer.close()


if __name__ == "__main__":
    asyncio.run(main())
