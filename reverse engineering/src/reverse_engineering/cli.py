"""CLI entry points for the reverse-engineering pipeline.

Usage (from the reverse engineering/ directory):
    python -m reverse_engineering.cli <command> [options]

Or via Makefile targets:
    make sync
    make phase1
    make phase2
    ...
    make phase7
    make gpu-check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync S3 Parquet partitions to local cache."""
    from reverse_engineering.config import get_settings
    from reverse_engineering.io.catalog import list_s3_partitions

    get_settings()
    partitions_per_feed = args.partitions

    from reverse_engineering.io.catalog import FEEDS

    for feed in FEEDS:
        available = list_s3_partitions(feed)
        if not available:
            log.warning("no_partitions_found", feed=feed)
            continue
        recent = available[-partitions_per_feed:]
        from reverse_engineering.io.s3_sync import sync_partitions

        paths = sync_partitions(recent)
        for p in paths:
            size_mb = Path(p).stat().st_size / (1024**2)
            log.info("synced", feed=feed, path=str(p), size_mb=round(size_mb, 2))

    return 0


def cmd_gpu_check(_args: argparse.Namespace) -> int:
    """Verify CUDA availability and RTX 3060 device."""
    try:
        import torch

        available = torch.cuda.is_available()
        device_name = torch.cuda.get_device_name(0) if available else "N/A"
        log.info("gpu_check", cuda_available=available, device=device_name)
        if not available:
            log.error("cuda_not_available", action="see BLOCKERS.md")
            return 1
        return 0
    except ImportError:
        log.error("torch_not_installed", action="pip install torch with cu124 wheel")
        return 1


def cmd_phase(phase_num: int, _args: argparse.Namespace) -> int:
    """Dispatch phase CLI entry points.

    Phases 1-7 are implemented in their respective modules.
    """
    log.info("phase_start", phase=phase_num)
    log.error(
        "phase_not_yet_implemented",
        phase=phase_num,
        note="Implement in the corresponding phase module",
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(
        prog="reverse_engineering",
        description="Reverse-engineering pipeline for @ohanism on Polymarket",
    )
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="Sync S3 partitions to local cache")
    p_sync.add_argument(
        "--partitions",
        type=int,
        default=1,
        help="Number of recent partitions per feed to sync (default: 1)",
    )

    sub.add_parser("gpu-check", help="Verify CUDA + RTX 3060 detected")

    for n in range(1, 8):
        sub.add_parser(f"phase{n}", help=f"Run Phase {n} pipeline")

    args = parser.parse_args(argv)

    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "gpu-check":
        return cmd_gpu_check(args)
    for n in range(1, 8):
        if args.command == f"phase{n}":
            return cmd_phase(n, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
