"""Unit tests for inventory.py — position tracking and exposure calculations."""

from __future__ import annotations

import polars as pl
import pytest

from reverse_engineering.tables.inventory import (
    build_inventory_series,
    compute_peak_inventory_per_market,
    compute_total_dollar_exposure_series,
)


def _make_fills(rows: list[dict]) -> pl.DataFrame:
    """Create minimal fills DataFrame for testing."""
    return pl.DataFrame(
        {
            "token_id": [r["token_id"] for r in rows],
            "block_number": [r.get("block_number", i + 1) for i, r in enumerate(rows)],
            "log_index": [r.get("log_index", 0) for r in rows],
            "t_block_ns": [
                r.get("t_block_ns", (i + 1) * 1_000_000_000) for i, r in enumerate(rows)
            ],
            "ohanism_side": [r["ohanism_side"] for r in rows],
            "size": [str(r["size"]) for r in rows],
            "price": [str(r["price"]) for r in rows],
        }
    )


class TestBuildInventorySeries:
    def test_single_buy_gives_positive_position(self) -> None:
        fills = _make_fills([{"token_id": "T1", "ohanism_side": "BUY", "size": 10.0, "price": 0.5}])
        inv = build_inventory_series(fills)
        assert len(inv) == 1
        assert float(inv["cum_position"][0]) == pytest.approx(10.0)
        assert float(inv["signed_size"][0]) == pytest.approx(10.0)

    def test_single_sell_gives_negative_position(self) -> None:
        fills = _make_fills([{"token_id": "T1", "ohanism_side": "SELL", "size": 5.0, "price": 0.6}])
        inv = build_inventory_series(fills)
        assert float(inv["cum_position"][0]) == pytest.approx(-5.0)
        assert float(inv["signed_size"][0]) == pytest.approx(-5.0)

    def test_cumulative_position_over_multiple_fills(self) -> None:
        fills = _make_fills(
            [
                {
                    "token_id": "T1",
                    "ohanism_side": "SELL",
                    "size": 10.0,
                    "price": 0.5,
                    "block_number": 1,
                },
                {
                    "token_id": "T1",
                    "ohanism_side": "SELL",
                    "size": 5.0,
                    "price": 0.5,
                    "block_number": 2,
                },
                {
                    "token_id": "T1",
                    "ohanism_side": "BUY",
                    "size": 3.0,
                    "price": 0.5,
                    "block_number": 3,
                },
            ]
        )
        inv = build_inventory_series(fills)
        positions = inv.sort("block_number")["cum_position"].to_list()
        assert positions[0] == pytest.approx(-10.0)
        assert positions[1] == pytest.approx(-15.0)
        assert positions[2] == pytest.approx(-12.0)

    def test_multiple_tokens_independent_positions(self) -> None:
        fills = _make_fills(
            [
                {
                    "token_id": "T1",
                    "ohanism_side": "SELL",
                    "size": 10.0,
                    "price": 0.5,
                    "block_number": 1,
                },
                {
                    "token_id": "T2",
                    "ohanism_side": "BUY",
                    "size": 8.0,
                    "price": 0.4,
                    "block_number": 2,
                },
                {
                    "token_id": "T1",
                    "ohanism_side": "BUY",
                    "size": 3.0,
                    "price": 0.5,
                    "block_number": 3,
                },
            ]
        )
        inv = build_inventory_series(fills)
        t1 = inv.filter(pl.col("token_id") == "T1").sort("block_number")
        t2 = inv.filter(pl.col("token_id") == "T2")
        assert t1["cum_position"].to_list() == pytest.approx([-10.0, -7.0])
        assert t2["cum_position"].to_list() == pytest.approx([8.0])

    def test_dollar_exposure_positive(self) -> None:
        fills = _make_fills(
            [{"token_id": "T1", "ohanism_side": "SELL", "size": 100.0, "price": 0.5}]
        )
        inv = build_inventory_series(fills)
        assert float(inv["dollar_exposure"][0]) == pytest.approx(50.0)


class TestComputePeakInventory:
    def test_single_token(self) -> None:
        fills = _make_fills(
            [
                {
                    "token_id": "T1",
                    "ohanism_side": "SELL",
                    "size": 20.0,
                    "price": 0.5,
                    "block_number": 1,
                },
                {
                    "token_id": "T1",
                    "ohanism_side": "BUY",
                    "size": 5.0,
                    "price": 0.5,
                    "block_number": 2,
                },
            ]
        )
        inv = build_inventory_series(fills)
        peaks = compute_peak_inventory_per_market(inv)
        assert len(peaks) == 1
        row = peaks.row(0, named=True)
        assert row["fill_count"] == 2
        assert row["peak_short"] == pytest.approx(-20.0)
        assert row["peak_long"] == pytest.approx(-15.0)
        assert row["peak_abs"] == pytest.approx(20.0)
        assert row["final_position"] == pytest.approx(-15.0)

    def test_net_zero_final_position(self) -> None:
        fills = _make_fills(
            [
                {
                    "token_id": "T1",
                    "ohanism_side": "SELL",
                    "size": 10.0,
                    "price": 0.5,
                    "block_number": 1,
                },
                {
                    "token_id": "T1",
                    "ohanism_side": "BUY",
                    "size": 10.0,
                    "price": 0.5,
                    "block_number": 2,
                },
            ]
        )
        inv = build_inventory_series(fills)
        peaks = compute_peak_inventory_per_market(inv)
        assert peaks["final_position"][0] == pytest.approx(0.0)
        assert peaks["peak_abs"][0] == pytest.approx(10.0)


class TestTotalDollarExposure:
    def test_single_token_exposure(self) -> None:
        fills = _make_fills(
            [{"token_id": "T1", "ohanism_side": "SELL", "size": 10.0, "price": 0.6}]
        )
        inv = build_inventory_series(fills)
        series = compute_total_dollar_exposure_series(inv)
        assert len(series) == 1
        assert float(series["total_dollar_exposure"][0]) == pytest.approx(6.0)

    def test_two_tokens_cumulative_exposure(self) -> None:
        fills = _make_fills(
            [
                {
                    "token_id": "T1",
                    "ohanism_side": "SELL",
                    "size": 10.0,
                    "price": 0.5,
                    "block_number": 1,
                },
                {
                    "token_id": "T2",
                    "ohanism_side": "SELL",
                    "size": 5.0,
                    "price": 0.4,
                    "block_number": 2,
                },
            ]
        )
        inv = build_inventory_series(fills)
        series = compute_total_dollar_exposure_series(inv).sort("block_number")
        # After fill 1: T1 = |10| * 0.5 = 5.0
        assert float(series["total_dollar_exposure"][0]) == pytest.approx(5.0)
        # After fill 2: T1=5.0 + T2=|5|*0.4=2.0 -> total=7.0
        assert float(series["total_dollar_exposure"][1]) == pytest.approx(7.0)
