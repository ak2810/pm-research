"""Test RawWriter: atomicity, rotation, queue-full drop."""
import gzip
import json
import time
from pathlib import Path

import pytest

from pm_research.storage.raw_writer import RawWriter


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


def _drain(writer: RawWriter, timeout: float = 3.0) -> None:
    """Wait for writer queue to drain."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if writer._queue.empty():
            time.sleep(0.05)
            return
        time.sleep(0.01)


def test_write_and_rotate_produces_valid_gzip(tmp_dir: Path) -> None:
    writer = RawWriter("test_feed", str(tmp_dir))
    for i in range(10):
        writer.write({"i": i, "v": "hello"})
    writer.close()

    gz_files = list(tmp_dir.rglob("*.jsonl.gz"))
    assert gz_files, "Expected at least one .jsonl.gz file"

    for gz_path in gz_files:
        with gzip.open(gz_path, "rt") as f:
            lines = f.readlines()
        assert lines, "gzip file must not be empty"
        for line in lines:
            obj = json.loads(line)
            assert "i" in obj


def test_no_tmp_files_after_close(tmp_dir: Path) -> None:
    writer = RawWriter("test_feed", str(tmp_dir))
    writer.write({"x": 1})
    writer.close()

    tmp_files = list(tmp_dir.rglob("*.tmp"))
    assert not tmp_files, f"Leftover .tmp files: {tmp_files}"


def test_queue_full_drops_and_logs(tmp_dir: Path, caplog: pytest.LogCaptureFixture) -> None:

    writer = RawWriter("test_feed", str(tmp_dir))
    # Flood queue beyond maxsize
    for i in range(15_000):
        writer.write({"i": i})
    assert writer._dropped > 0
    writer.close()


def test_all_events_written_when_queue_not_full(tmp_dir: Path) -> None:
    n = 100
    writer = RawWriter("test_feed", str(tmp_dir))
    for i in range(n):
        writer.write({"seq": i})
    writer.close()

    gz_files = list(tmp_dir.rglob("*.jsonl.gz"))
    total = 0
    for gz_path in gz_files:
        with gzip.open(gz_path, "rt") as f:
            total += sum(1 for _ in f)
    assert total == n
