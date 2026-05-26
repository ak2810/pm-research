"""Phase 4 gate: JSONL.gz → Parquet → DataFrame round-trip.

Verifies: row count preserved, ns timestamps survive, Decimal precision intact.
"""
import gzip
import json
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from pm_research.pipeline.jsonl_to_parquet import convert_file
from pm_research.pipeline.parquet_schemas import POLYGON_ORDER_FILLED


@pytest.fixture()
def sample_gz(tmp_path: Path) -> Path:
    rows = [
        {
            "feed": "polygon",
            "t_recv_ns": 1_779_782_571_250_000_000,
            "block_number": 65_000_000,
            "block_hash": "0xabc",
            "tx_hash": "0xdef",
            "log_index": 0,
            "event": "OrderFilled",
            "order_hash": "0x123",
            "maker": "0xmaker",
            "taker": "0xtaker",
            "side": 0,
            "token_id": "12345",
            "maker_amount_raw": "5000000",
            "maker_amount_decimal": "5.000000",
            "taker_amount_raw": "5000000",
            "taker_amount_decimal": "5.000000",
            "fee_raw": "50000",
            "fee_decimal": "0.050000",
            "builder": "00" * 32,
            "metadata": "ff" * 32,
            "exchange": "0xe111",
        }
        for _ in range(50)
    ]
    gz_path = tmp_path / "data.jsonl.gz"
    with gzip.open(gz_path, "wt") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return gz_path


def test_round_trip_row_count(sample_gz: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.parquet"
    failures = convert_file(sample_gz, out, POLYGON_ORDER_FILLED)
    assert failures == 0
    df = pl.read_parquet(out)
    assert len(df) == 50


def test_timestamp_ns_preserved(sample_gz: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.parquet"
    convert_file(sample_gz, out, POLYGON_ORDER_FILLED)
    df = pl.read_parquet(out)
    assert df["t_recv_ns"][0] == 1_779_782_571_250_000_000


def test_decimal_precision(sample_gz: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.parquet"
    convert_file(sample_gz, out, POLYGON_ORDER_FILLED)
    df = pl.read_parquet(out)
    # maker_amount_decimal should be parseable to Decimal without float loss
    val = str(df["maker_amount_decimal"][0])
    assert Decimal(val) == Decimal("5")


def test_malformed_line_counted_not_aborted(tmp_path: Path) -> None:
    gz_path = tmp_path / "bad.jsonl.gz"
    with gzip.open(gz_path, "wt") as f:
        f.write('{"feed": "polygon", "t_recv_ns": 1}\n')
        f.write("not json {\n")
        f.write('{"feed": "polygon", "t_recv_ns": 2}\n')
    out = tmp_path / "bad.parquet"
    failures = convert_file(gz_path, out, POLYGON_ORDER_FILLED)
    assert failures == 1
