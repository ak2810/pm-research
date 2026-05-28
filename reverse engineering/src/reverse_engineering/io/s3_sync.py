"""Sync S3 Parquet partitions to local cache, with size-cap eviction.

Memory strategy: metadata and small chunks only. Data files are written
directly to disk via boto3 streaming download. Peak RAM: <50 MB.

Cache cap: controlled by Settings.cache_max_gb (default 200 GB).
When exceeded, evict oldest partitions (by directory mtime) until under cap.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import boto3
import structlog

from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import FEEDS, Partition, list_s3_partitions

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


def _cache_size_gb() -> float:
    """Return total size of local Parquet cache in GB."""
    cfg = get_settings()
    if not cfg.cache_dir.exists():
        return 0.0
    total = sum(f.stat().st_size for f in cfg.cache_dir.rglob("*") if f.is_file())
    return total / (1024**3)


def _evict_oldest(target_gb: float) -> None:
    """Evict oldest partitions until cache is below target_gb.

    Oldest = partition directory with smallest mtime.
    """
    cfg = get_settings()
    hour_dirs = sorted(
        cfg.cache_dir.glob("feed=*/date=*/hour=*"),
        key=lambda p: p.stat().st_mtime,
    )
    for hour_dir in hour_dirs:
        if _cache_size_gb() <= target_gb:
            break
        log.warning("evicting_partition", path=str(hour_dir))
        shutil.rmtree(hour_dir, ignore_errors=True)
        # Clean up empty parent date dirs
        date_dir = hour_dir.parent
        if date_dir.exists() and not any(date_dir.iterdir()):
            date_dir.rmdir()


def download_partition(partition: Partition, overwrite: bool = False) -> Path:
    """Download one S3 partition to local cache.

    Args:
        partition: The partition to download.
        overwrite: If True, overwrite existing local file.

    Returns:
        Local path of the downloaded file.

    Raises:
        RuntimeError: If download fails.
    """
    cfg = get_settings()
    local_path = partition.local_path

    if local_path.exists() and not overwrite:
        log.debug("partition_already_cached", path=str(local_path))
        return local_path

    local_path.parent.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client(
        "s3",
        region_name=cfg.aws_default_region,
        aws_access_key_id=cfg.aws_access_key_id or None,
        aws_secret_access_key=cfg.aws_secret_access_key or None,
    )

    try:
        s3.download_file(
            Bucket=cfg.s3_bucket,
            Key=partition.s3_key,
            Filename=str(local_path),
        )
    except Exception as exc:
        local_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to download s3://{cfg.s3_bucket}/{partition.s3_key}: {exc}"
        ) from exc

    size_mb = local_path.stat().st_size / (1024**2)
    log.info(
        "partition_downloaded",
        feed=partition.feed,
        date=partition.date,
        hour=partition.hour,
        size_mb=round(size_mb, 2),
    )
    return local_path


def sync_partitions(
    partitions: list[Partition],
    overwrite: bool = False,
) -> list[Path]:
    """Sync a list of partitions to local cache, evicting if cap exceeded.

    Args:
        partitions: Partitions to sync.
        overwrite: Overwrite existing local files.

    Returns:
        List of local paths for successfully synced partitions.
    """
    cfg = get_settings()
    paths: list[Path] = []

    for partition in partitions:
        path = download_partition(partition, overwrite=overwrite)
        paths.append(path)

        current_gb = _cache_size_gb()
        if current_gb > cfg.cache_max_gb:
            log.warning(
                "cache_cap_exceeded",
                current_gb=round(current_gb, 2),
                cap_gb=cfg.cache_max_gb,
            )
            _evict_oldest(target_gb=cfg.cache_max_gb * 0.9)

    return paths


def sync_one_per_feed(date: str, hour: int) -> dict[str, Path]:
    """Sync one partition per feed for the given date and hour.

    Used by `make sync` to validate S3 connectivity and cache operation.

    Args:
        date: YYYY-MM-DD
        hour: 0-23

    Returns:
        Dict mapping feed name → local path (only feeds successfully synced).
    """
    result: dict[str, Path] = {}
    for feed in FEEDS:
        partitions_on_s3 = list_s3_partitions(feed, date=date)
        target = next((p for p in partitions_on_s3 if p.hour == hour), None)
        if target is None:
            log.warning(
                "partition_not_found_in_s3",
                feed=feed,
                date=date,
                hour=hour,
            )
            continue
        try:
            path = download_partition(target)
            result[feed] = path
        except RuntimeError as exc:
            log.error(
                "sync_failed",
                feed=feed,
                date=date,
                hour=hour,
                error=str(exc),
            )
    return result
