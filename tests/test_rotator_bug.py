"""
Regression test: rotator schema bug.

Proves:
  BUGGY  — applying PM_CLOB_BOOK to all events drops price/size/side data.
  FIXED  — schema={} (inferred) preserves all fields for all event types.

Run: py tests/test_rotator_bug.py
"""
import gzip
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl

from pm_research.pipeline.jsonl_to_parquet import convert_file
from pm_research.pipeline.parquet_schemas import PM_CLOB_BOOK

FIXTURE = Path(__file__).parent / "fixtures" / "pm_clob_sample.jsonl.gz"


def inspect_raw() -> dict[str, int]:
    event_types: dict[str, int] = {}
    sample_pc = None
    with gzip.open(FIXTURE, "rt") as f:
        for line in f:
            r = json.loads(line)
            et = r.get("event_type", r.get("event", "NONE"))
            event_types[et] = event_types.get(et, 0) + 1
            if et == "price_change" and sample_pc is None:
                sample_pc = r
    print("Raw event types:", event_types)
    if sample_pc:
        print("price_change keys:", list(sample_pc.keys()))
        if "price_changes" in sample_pc and sample_pc["price_changes"]:
            print("price_changes[0]:", sample_pc["price_changes"][0])
    return event_types


def run_and_report(schema: dict, label: str) -> pl.DataFrame:
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        dst = Path(tmp.name)
    convert_file(FIXTURE, dst, schema)
    df = pl.read_parquet(dst)
    dst.unlink(missing_ok=True)

    pc = df.filter(pl.col("event_type") == "price_change") if "event_type" in df.columns else df.head(0)
    print(f"\n{'='*60}")
    print(f"{label}")
    print(f"{'='*60}")
    print(f"  Total rows : {len(df)}")
    print(f"  price_change rows : {len(pc)}")
    print(f"  Columns : {df.columns}")
    if len(pc) > 0:
        row = pc.head(1).to_dicts()[0]
        print("  Sample price_change row:")
        for k, v in row.items():
            print(f"    {k:<25} = {repr(str(v))[:70]}")
    return df


def assert_buggy(df: pl.DataFrame) -> None:
    pc = df.filter(pl.col("event_type") == "price_change")
    assert len(pc) > 0, "no price_change rows found"
    row = pc.head(1).to_dicts()[0]
    assert row.get("bids_json") is None, "BUG not present: bids_json should be None"
    assert row.get("asks_json") is None, "BUG not present: asks_json should be None"
    assert "price_changes" not in row, "price_changes column should not exist in buggy output"
    print("  [PASS] Bug confirmed: price/size fields are None in buggy output")


def assert_fixed(df: pl.DataFrame) -> None:
    pc = df.filter(pl.col("event_type") == "price_change")
    assert len(pc) > 0, "no price_change rows found"
    assert "price_changes" in df.columns, "price_changes column missing from fixed output"
    row = pc.head(1).to_dicts()[0]
    val = row.get("price_changes")
    assert val is not None, "price_changes is None — data still lost"
    parsed = json.loads(val)
    assert len(parsed) > 0, "price_changes is empty list"
    assert "price" in parsed[0], "price key missing from price_changes entry"
    assert "size" in parsed[0], "size key missing from price_changes entry"
    assert "side" in parsed[0], "side key missing from price_changes entry"
    assert "best_bid" in parsed[0], "best_bid key missing from price_changes entry"
    assert "best_ask" in parsed[0], "best_ask key missing from price_changes entry"
    print(f"  [PASS] Fix confirmed: price_changes preserved with price={parsed[0]['price']} size={parsed[0]['size']} side={parsed[0]['side']} best_bid={parsed[0]['best_bid']} best_ask={parsed[0]['best_ask']}")


if __name__ == "__main__":
    print("=" * 60)
    print("STEP 1 — Inspect raw JSONL")
    print("=" * 60)
    inspect_raw()

    buggy_df = run_and_report(PM_CLOB_BOOK, "BUGGY — PM_CLOB_BOOK applied to all event types")
    assert_buggy(buggy_df)

    fixed_df = run_and_report({}, "FIXED — schema={} inferred, all event types preserved")
    assert_fixed(fixed_df)

    print("\n[ALL ASSERTIONS PASSED]")
