"""Atomic hourly-rotating gzipped JSONL writer.

Write path: events → bounded queue (10000) → background thread →
.tmp file → fsync → rename (atomic). One file per hour per feed.

Critical invariant: a completed rotation always produces a valid gzip file,
even if the process is killed mid-write (the .tmp is abandoned, not the
completed renamed file).
"""
import gzip
import json
import os
import queue
import threading
import time
from pathlib import Path

from pm_research.logging import get_logger

log = get_logger(__name__)

_QUEUE_MAXSIZE = 10_000
_SENTINEL = object()


class RawWriter:
    def __init__(self, feed: str, data_dir: str) -> None:
        self._feed = feed
        self._base = Path(data_dir) / feed
        self._base.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[object] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"writer-{feed}")
        self._thread.start()

    def write(self, event: dict[str, object]) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            log.warning("writer_queue_full", feed=self._feed, dropped_total=self._dropped)

    def close(self) -> None:
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=30)

    def _run(self) -> None:
        current_hour: str | None = None
        gz: gzip.GzipFile | None = None
        tmp_path: Path | None = None
        final_path: Path | None = None

        try:
            while True:
                try:
                    item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    # Check if hour rolled even with no events
                    hour = _current_hour()
                    if current_hour is not None and hour != current_hour:
                        gz, tmp_path, final_path = _rotate(
                            gz, tmp_path, final_path, self._feed
                        )
                        current_hour = hour
                        gz, tmp_path, final_path = _open_new(self._base, hour, self._feed)
                    continue

                if item is _SENTINEL:
                    break

                hour = _current_hour()
                if current_hour is None or hour != current_hour:
                    if current_hour is not None:
                        gz, tmp_path, final_path = _rotate(
                            gz, tmp_path, final_path, self._feed
                        )
                    gz, tmp_path, final_path = _open_new(self._base, hour, self._feed)
                    current_hour = hour

                assert gz is not None
                line = json.dumps(item, separators=(",", ":")) + "\n"
                gz.write(line.encode())

        finally:
            if gz is not None:
                _rotate(gz, tmp_path, final_path, self._feed)


def _current_hour() -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime())


def _open_new(
    base: Path, hour: str, feed: str
) -> tuple[gzip.GzipFile, Path, Path]:
    date = hour[:10]
    h = hour[11:13]
    dest_dir = base / f"date={date}" / f"hour={h}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / "data.jsonl.gz"
    tmp = dest_dir / "data.jsonl.gz.tmp"
    raw = tmp.open("wb")
    gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6)
    log.info("writer_opened", feed=feed, path=str(tmp))
    return gz, tmp, final


def _rotate(
    gz: gzip.GzipFile | None,
    tmp_path: Path | None,
    final_path: Path | None,
    feed: str,
) -> tuple[None, None, None]:
    if gz is None or tmp_path is None or final_path is None:
        return None, None, None
    try:
        gz.close()
        # fsync the underlying file before rename
        with tmp_path.open("ab") as f:
            os.fsync(f.fileno())
        tmp_path.rename(final_path)
        log.info("writer_rotated", feed=feed, path=str(final_path))
    except Exception as exc:
        log.error("writer_rotate_failed", feed=feed, error=str(exc))
    return None, None, None
