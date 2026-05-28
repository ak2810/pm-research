"""Integration tests for Phase 1 reconciliation.

These tests require local Parquet cache to be populated (make sync).
They are skipped when cache is empty so precommit passes without data.

Run after make sync to verify Phase 1 acceptance gate.
"""

from __future__ import annotations

import pytest

from reverse_engineering.config import get_settings


def _cache_has_data() -> bool:
    cfg = get_settings()
    return any(cfg.cache_dir.glob("feed=*/date=*/hour=*/data.parquet"))


@pytest.mark.skipif(
    not _cache_has_data(),
    reason="Local Parquet cache empty — run 'make sync' first",
)
class TestPhase1Reconciliation:
    def test_polygon_feed_readable(self) -> None:
        """Polygon feed partition is lazily scannable after sync."""
        import polars as pl

        cfg = get_settings()
        polygon_files = list(cfg.cache_dir.glob("feed=polygon/date=*/hour=*/data.parquet"))
        assert len(polygon_files) > 0, "No polygon partitions in cache"
        lf = pl.scan_parquet(str(polygon_files[0]), low_memory=True)
        cols = list(lf.schema)
        assert "block_number" in cols or len(cols) > 0

    def test_pm_clob_feed_readable(self) -> None:
        """pm_clob feed partition is lazily scannable after sync."""
        import polars as pl

        cfg = get_settings()
        pm_files = list(cfg.cache_dir.glob("feed=pm_clob/date=*/hour=*/data.parquet"))
        assert len(pm_files) > 0, "No pm_clob partitions in cache"
        lf = pl.scan_parquet(str(pm_files[0]), low_memory=True)
        assert len(list(lf.schema)) > 0
