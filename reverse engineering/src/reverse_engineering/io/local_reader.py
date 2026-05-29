"""Lazy Polars readers over the local Parquet cache.

All reads use pl.scan_parquet (lazy API) with predicate and projection pushdown
to minimize memory. Never materialize a full feed-day.

Memory strategy: scan_parquet + filter/select before collect. For joins that
need multiple hours, use streaming=True. Peak RAM depends on the query slice
but is kept well below 48 GB by design.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
import structlog

from reverse_engineering.config import get_settings

if TYPE_CHECKING:
    from reverse_engineering.io.catalog import Partition

log = structlog.get_logger(__name__)


def scan_feed(
    feed: str,
    date: str,
    hour: int | None = None,
    columns: list[str] | None = None,
) -> pl.LazyFrame:
    """Lazily scan a feed for a given date (and optional hour).

    Uses predicate + projection pushdown. Call .collect() or
    .collect(streaming=True) on the result.

    Args:
        feed: Feed name (pm_clob, polygon, binance, pm_meta).
        date: YYYY-MM-DD
        hour: If None, scan all hours for this date.
        columns: If provided, only read these columns (projection pushdown).

    Returns:
        LazyFrame — nothing materialized yet.

    Raises:
        FileNotFoundError: If no local Parquet files found for the query.
    """
    cfg = get_settings()
    feed_dir = cfg.cache_dir / f"feed={feed}" / f"date={date}"

    if hour is not None:
        pattern = str(feed_dir / f"hour={hour:02d}" / "data.parquet")
        paths = [Path(pattern)]
    else:
        paths = sorted(feed_dir.glob("hour=*/data.parquet"))

    existing = [p for p in paths if p.exists()]
    if not existing:
        raise FileNotFoundError(f"No cached Parquet found for feed={feed} date={date} hour={hour}")

    lf = pl.scan_parquet(
        [str(p) for p in existing],
        low_memory=True,
        hive_partitioning=False,
        use_statistics=False,
    )

    if columns:
        available = list(lf.schema)
        cols = [c for c in columns if c in available]
        lf = lf.select(cols)

    return lf


def scan_partition(
    partition: Partition,
    columns: list[str] | None = None,
) -> pl.LazyFrame:
    """Lazily scan a single local partition.

    Args:
        partition: The partition to scan.
        columns: If provided, only read these columns.

    Returns:
        LazyFrame.

    Raises:
        FileNotFoundError: If partition not in local cache.
    """
    if not partition.exists_locally():
        raise FileNotFoundError(f"Partition not cached locally: {partition.local_path}")
    lf = pl.scan_parquet(
        str(partition.local_path), low_memory=True, hive_partitioning=False, use_statistics=False
    )
    if columns:
        available = list(lf.schema)
        cols = [c for c in columns if c in available]
        lf = lf.select(cols)
    return lf


def collect_filtered(
    feed: str,
    date: str,
    hour: int | None = None,
    *,
    token_ids: list[str] | None = None,
    columns: list[str] | None = None,
    streaming: bool = False,
) -> pl.DataFrame:
    """Materialize a filtered slice of a feed.

    Applies predicate pushdown on `asset_id` / `token_id` before materialization.

    Args:
        feed: Feed name.
        date: YYYY-MM-DD
        hour: If None, scan all hours.
        token_ids: If provided, filter rows to these token_ids (any column
            matching asset_id / token_id).
        columns: If provided, select only these columns.
        streaming: Use Polars streaming engine (lower peak RAM for large
            group-bys; slightly slower).

    Returns:
        Materialized DataFrame.
    """
    lf = scan_feed(feed, date, hour, columns=columns)

    if token_ids:
        schema_names = set(lf.schema)
        id_col: str | None = None
        for candidate in ("asset_id", "token_id"):
            if candidate in schema_names:
                id_col = candidate
                break
        if id_col:
            lf = lf.filter(pl.col(id_col).is_in(token_ids))

    result: pl.DataFrame = lf.collect(streaming=streaming)
    log.debug(
        "feed_collected",
        feed=feed,
        date=date,
        hour=hour,
        rows=len(result),
        streaming=streaming,
    )
    return result
