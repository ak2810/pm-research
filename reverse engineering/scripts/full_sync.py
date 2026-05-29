"""A2: Sync all S3 partitions for the full analysis window.

Full window: 2026-05-27 hour=04 through 2026-05-29 hour=04 (49 hours).
Drop hour=03 on 2026-05-27 (first recording hour, warmup/backfill risk).

Downloads only partitions not already in local cache.
Retries on 429/5xx with exponential backoff.
Logs progress and final sizes.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import polars as pl
import structlog

from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import FEEDS, list_s3_partitions, Partition
from reverse_engineering.io.s3_sync import download_partition, _cache_size_gb

log = structlog.get_logger(__name__)
cfg = get_settings()

WINDOW_START = ("2026-05-27", 4)   # inclusive, drop hour=03
WINDOW_END   = ("2026-05-29", 4)   # inclusive

def in_window(p: Partition) -> bool:
    lo = (WINDOW_START[0], WINDOW_START[1])
    hi = (WINDOW_END[0], WINDOW_END[1])
    key = (p.date, p.hour)
    return lo <= key <= hi

t0 = time.time()
stats: dict[str, dict] = {}
total_downloaded = 0
total_skipped = 0

for feed in FEEDS:
    log.info("feed_sync_start", feed=feed)
    all_s3 = list_s3_partitions(feed)
    window_parts = [p for p in all_s3 if in_window(p)]
    missing = [p for p in window_parts if not p.exists_locally()]

    log.info("feed_partitions", feed=feed, window=len(window_parts), missing=len(missing))

    feed_downloaded = 0
    feed_skipped = len(window_parts) - len(missing)

    for i, p in enumerate(missing):
        attempt = 0
        while attempt < 5:
            try:
                download_partition(p, overwrite=False)
                feed_downloaded += 1
                if (i + 1) % 5 == 0:
                    gb = _cache_size_gb()
                    log.info("feed_progress",
                             feed=feed, done=i+1, total=len(missing), cache_gb=round(gb, 2))
                break
            except Exception as exc:
                attempt += 1
                wait = 2 ** attempt
                log.warning("download_retry", feed=feed, partition=f"{p.date}/{p.hour:02d}",
                            attempt=attempt, wait_s=wait, error=str(exc)[:80])
                time.sleep(wait)

    stats[feed] = {"window": len(window_parts), "downloaded": feed_downloaded,
                   "skipped": feed_skipped}
    total_downloaded += feed_downloaded
    total_skipped += feed_skipped

elapsed = time.time() - t0
cache_gb = _cache_size_gb()

print(f"\n=== A2 SYNC COMPLETE in {elapsed/60:.1f} min ===")
print(f"Cache size: {cache_gb:.1f} GB")
for feed, s in stats.items():
    print(f"  {feed}: window={s['window']} downloaded={s['downloaded']} skipped={s['skipped']}")
print(f"Total new downloads: {total_downloaded}, skipped (already cached): {total_skipped}")

# Verify all partitions readable
print("\nVerifying all partitions readable...")
errors = 0
for feed in FEEDS:
    all_s3 = list_s3_partitions(feed)
    window_parts = [p for p in all_s3 if in_window(p)]
    for p in window_parts:
        if not p.exists_locally():
            print(f"  MISSING: {feed} {p.date} hour={p.hour}")
            errors += 1
            continue
        try:
            lf = pl.scan_parquet(str(p.local_path), low_memory=True, hive_partitioning=False)
            _ = list(lf.schema)
        except Exception as exc:
            print(f"  UNREADABLE: {feed} {p.date} hour={p.hour}: {exc}")
            errors += 1

if errors == 0:
    print("All partitions present and readable.")
else:
    print(f"{errors} errors found. Check BLOCKERS.md.")
