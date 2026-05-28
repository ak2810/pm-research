"""Discovery of available date/hour partitions in S3 and local cache.

Memory strategy: metadata-only reads; no data materialized. Peak RAM: <1 MB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
import structlog

from reverse_engineering.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_DATE_HOUR_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})/hour=(\d{2})")

FEEDS = ("pm_clob", "polygon", "binance", "pm_meta")


@dataclass(frozen=True)
class Partition:
    feed: str
    date: str  # YYYY-MM-DD
    hour: int  # 0-23

    @property
    def s3_key(self) -> str:
        cfg = get_settings()
        return (
            f"{cfg.s3_prefix}/feed={self.feed}/"
            f"date={self.date}/hour={self.hour:02d}/data.parquet"
        )

    @property
    def local_path(self) -> Path:
        cfg = get_settings()
        return (
            cfg.cache_dir
            / f"feed={self.feed}"
            / f"date={self.date}"
            / f"hour={self.hour:02d}"
            / "data.parquet"
        )

    def exists_locally(self) -> bool:
        return self.local_path.exists()


def list_s3_partitions(feed: str, date: str | None = None) -> list[Partition]:
    """List all available partitions for a feed in S3.

    Args:
        feed: Feed name (pm_clob, polygon, binance, pm_meta).
        date: If provided, only list partitions for this date (YYYY-MM-DD).

    Returns:
        Sorted list of Partition objects.
    """
    cfg = get_settings()
    s3 = boto3.client(
        "s3",
        region_name=cfg.aws_default_region,
        aws_access_key_id=cfg.aws_access_key_id or None,
        aws_secret_access_key=cfg.aws_secret_access_key or None,
    )

    prefix = f"{cfg.s3_prefix}/feed={feed}/"
    if date:
        prefix += f"date={date}/"

    paginator = s3.get_paginator("list_objects_v2")
    partitions: list[Partition] = []

    for page in paginator.paginate(Bucket=cfg.s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            if not key.endswith("data.parquet"):
                continue
            m = _DATE_HOUR_RE.search(key)
            if not m:
                continue
            partitions.append(Partition(feed=feed, date=m.group(1), hour=int(m.group(2))))

    partitions.sort(key=lambda p: (p.date, p.hour))
    log.info(
        "s3_partition_list_complete",
        feed=feed,
        date=date,
        count=len(partitions),
    )
    return partitions


def list_local_partitions(feed: str) -> list[Partition]:
    """List partitions present in local cache.

    Args:
        feed: Feed name.

    Returns:
        Sorted list of cached Partition objects.
    """
    cfg = get_settings()
    feed_dir = cfg.cache_dir / f"feed={feed}"
    partitions: list[Partition] = []

    if not feed_dir.exists():
        return partitions

    for parquet_file in feed_dir.glob("date=*/hour=*/data.parquet"):
        m = _DATE_HOUR_RE.search(str(parquet_file))
        if not m:
            continue
        partitions.append(Partition(feed=feed, date=m.group(1), hour=int(m.group(2))))

    partitions.sort(key=lambda p: (p.date, p.hour))
    return partitions


def latest_local_partition(feed: str) -> Partition | None:
    """Return the most recent locally-cached partition for a feed."""
    parts = list_local_partitions(feed)
    return parts[-1] if parts else None
