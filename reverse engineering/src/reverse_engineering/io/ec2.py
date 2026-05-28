"""EC2 helpers: collector health check and live .tmp file pull.

EC2 is a DATA SOURCE only — never a compute node. These helpers:
  1. SSH health check: confirm collectors are running.
  2. SCP pull: download the current-hour .jsonl.gz.tmp for sub-hour-fresh data.

Uses subprocess with the system ssh/scp. Requires the key at
Settings.ec2_key_path to be present and readable.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import structlog

from reverse_engineering.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_COLLECTORS = (
    "pm-clob-collector",
    "pm-polygon-indexer",
    "pm-binance-collector",
    "pm-metadata-snapshotter",
)


def check_collector_health() -> dict[str, str]:
    """SSH to EC2 and check systemctl status for all collectors.

    Returns:
        Dict mapping collector service name → status string
        (e.g. "active", "inactive", "failed").

    Raises:
        RuntimeError: If SSH connection fails.
    """
    cfg = get_settings()
    statuses: dict[str, str] = {}

    for svc in _COLLECTORS:
        cmd = [
            "ssh",
            "-i",
            cfg.ec2_key_path,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            cfg.ec2_host,
            f"systemctl is-active {svc} 2>/dev/null || echo unknown",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            status = result.stdout.strip()
            statuses[svc] = status
            log.info("collector_health", service=svc, status=status)
        except subprocess.TimeoutExpired:
            statuses[svc] = "timeout"
            log.warning("collector_health_timeout", service=svc)
        except Exception as exc:
            raise RuntimeError(f"SSH health check failed for {svc}: {exc}") from exc

    return statuses


def pull_live_tmp(
    feed: str,
    date: str,
    hour: int,
    dest_dir: Path | None = None,
) -> Path:
    """SCP the current-hour .jsonl.gz.tmp from EC2 for sub-hour-fresh data.

    Use only when you need data fresher than the last S3 rotation.

    Args:
        feed: Feed name (pm_clob, polygon, binance, pm_meta).
        date: YYYY-MM-DD
        hour: 0-23
        dest_dir: Local destination directory. Defaults to output/cache/tmp/.

    Returns:
        Local path of the downloaded .tmp file.

    Raises:
        RuntimeError: If SCP fails.
    """
    cfg = get_settings()
    if dest_dir is None:
        dest_dir = cfg.cache_dir / "tmp"
    dest_dir.mkdir(parents=True, exist_ok=True)

    remote_path = f"/var/pm-research/data/{feed}/date={date}/hour={hour:02d}" f"/data.jsonl.gz.tmp"
    local_file = dest_dir / f"{feed}_date={date}_hour={hour:02d}.jsonl.gz.tmp"

    cmd = [
        "scp",
        "-i",
        cfg.ec2_key_path,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        f"{cfg.ec2_host}:{remote_path}",
        str(local_file),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"SCP failed (rc={result.returncode}): {result.stderr}")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SCP timed out pulling {remote_path}") from exc

    size_mb = local_file.stat().st_size / (1024**2)
    log.info(
        "tmp_file_pulled",
        feed=feed,
        date=date,
        hour=hour,
        local=str(local_file),
        size_mb=round(size_mb, 2),
    )
    return local_file
