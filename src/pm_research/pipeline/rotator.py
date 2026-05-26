"""Pipeline rotator: scan completed JSONL.gz files → Parquet → S3 → delete local.

Runs every 5 minutes. Only processes files NOT named data.jsonl.gz.tmp (in-progress).
An hour's data.jsonl.gz is "complete" if its hour < current UTC hour.
"""
import asyncio
import re
from pathlib import Path
from typing import Any

from pm_research.logging import get_logger
from pm_research.pipeline.jsonl_to_parquet import convert_file
from pm_research.storage.s3 import S3Uploader

log = get_logger(__name__)

_FEED_SCHEMAS: dict[str, Any] = {
    "pm_clob": {},   # mixed event types — inferred schema preserves all fields
    "polygon": {},   # mixed event types — inferred schema preserves all fields
    "binance": {},   # mixed stream types — inferred schema preserves all fields
}

_PATH_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})/hour=(\d{2})/data\.jsonl\.gz$")


class PipelineRotator:
    def __init__(
        self,
        data_dir: str,
        s3_bucket: str,
        s3_region: str = "eu-west-1",
        interval_s: int = 300,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._uploader = S3Uploader(s3_bucket, s3_region)
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="pipeline-rotator")

    async def stop(self) -> None:
        import contextlib

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._rotate_all)
            except Exception as exc:
                log.error("rotator_error", error=str(exc))
            await asyncio.sleep(self._interval)

    def _rotate_all(self) -> None:
        import time

        current_hour = time.strftime("%H", time.gmtime())
        current_date = time.strftime("%Y-%m-%d", time.gmtime())

        for gz_path in self._data_dir.rglob("data.jsonl.gz"):
            m = _PATH_RE.search(str(gz_path))
            if not m:
                continue
            date, hour = m.group(1), m.group(2)
            # Skip current hour's file (still being written)
            if date == current_date and hour == current_hour:
                continue
            # Skip .tmp files
            if gz_path.suffix == ".tmp":
                continue

            feed = gz_path.parts[-4]  # /.../{feed}/date=.../hour=.../data.jsonl.gz
            self._process_file(gz_path, feed, date, hour)

    def _process_file(self, gz_path: Path, feed: str, date: str, hour: str) -> None:
        parquet_path = gz_path.with_suffix("").with_suffix(".parquet")
        s3_key = f"raw/feed={feed}/date={date}/hour={hour}/data.parquet"

        # Pick schema — use first matching schema for the feed
        schemas = _FEED_SCHEMAS.get(feed, {})
        schema = next(iter(schemas.values())) if schemas else {}

        try:
            failures = convert_file(gz_path, parquet_path, schema)
            self._uploader.upload_parquet(parquet_path, s3_key)
            gz_path.unlink()
            log.info(
                "rotated",
                feed=feed,
                date=date,
                hour=hour,
                failures=failures,
                s3_key=s3_key,
            )
        except Exception as exc:
            log.error("rotate_failed", gz=str(gz_path), error=str(exc))
            if parquet_path.exists():
                parquet_path.unlink()
