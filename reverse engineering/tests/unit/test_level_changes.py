"""Unit tests for level_changes.py — price-format fix and matching logic.

Regression test for the 6dp / variable-dp mismatch:
pm_clob stores prices as "0.61", "0.50", "0.010" etc. (variable decimal places).
ohanism_fills stores prices as "0.610000", "0.500000" etc. (exactly 6dp).
Both must normalize to 6dp before matching.
"""

from __future__ import annotations

import json

import polars as pl

from reverse_engineering.tables.level_changes import (
    _explode_price_changes,
    _normalize_price,
    match_fills_to_level_changes,
)

# ── _normalize_price ─────────────────────────────────────────────────────────


class TestNormalizePrice:
    def test_two_dp_to_six(self) -> None:
        assert _normalize_price("0.61") == "0.610000"

    def test_already_six_dp(self) -> None:
        assert _normalize_price("0.610000") == "0.610000"

    def test_one_dp(self) -> None:
        assert _normalize_price("0.5") == "0.500000"

    def test_integer(self) -> None:
        assert _normalize_price("1") == "1.000000"

    def test_zero(self) -> None:
        assert _normalize_price("0") == "0.000000"

    def test_rounding(self) -> None:
        # Should round, not truncate
        assert _normalize_price("0.6100004") == "0.610000"

    def test_bad_input_returns_raw(self) -> None:
        result = _normalize_price("not_a_number")
        assert result == "not_a_number"


# ── _explode_price_changes ───────────────────────────────────────────────────


def _make_pm_clob_row(
    t_ns: int,
    entries: list[dict],
) -> pl.DataFrame:
    """Create a minimal pm_clob DataFrame with one price_change row."""
    return pl.DataFrame(
        {
            "event_type": ["price_change"],
            "price_changes": [json.dumps(entries)],
            "t_recv_ns": [t_ns],
        }
    )


class TestExplodePriceChanges:
    def test_normalizes_price_to_six_dp(self) -> None:
        """Prices "0.61" and "0.5" should both become 6dp strings."""
        df = _make_pm_clob_row(
            1_000_000_000,
            [
                {"asset_id": "TOKEN1", "price": "0.61", "side": "SELL", "size": "100", "hash": "a"},
                {"asset_id": "TOKEN1", "price": "0.5", "side": "BUY", "size": "50", "hash": "b"},
            ],
        )
        result = _explode_price_changes(df, {"TOKEN1"})
        prices = result["price"].to_list()
        assert "0.610000" in prices
        assert "0.500000" in prices
        for p in prices:
            assert len(p.split(".")[-1]) == 6, f"price {p!r} not 6dp"

    def test_filters_by_token_id(self) -> None:
        df = _make_pm_clob_row(
            1_000_000_000,
            [
                {"asset_id": "TOKEN1", "price": "0.50", "side": "SELL", "size": "10", "hash": "a"},
                {"asset_id": "TOKEN2", "price": "0.60", "side": "BUY", "size": "20", "hash": "b"},
            ],
        )
        result = _explode_price_changes(df, {"TOKEN1"})
        assert result["token_id"].to_list() == ["TOKEN1"]

    def test_empty_when_no_matching_tokens(self) -> None:
        df = _make_pm_clob_row(
            1_000_000_000,
            [{"asset_id": "OTHER", "price": "0.50", "side": "SELL", "size": "10", "hash": "a"}],
        )
        result = _explode_price_changes(df, {"TOKEN1"})
        assert result.is_empty()

    def test_handles_null_price_changes(self) -> None:
        df = pl.DataFrame(
            {
                "event_type": ["price_change"],
                "price_changes": [None],
                "t_recv_ns": [1_000_000_000],
            }
        )
        result = _explode_price_changes(df, {"TOKEN1"})
        assert result.is_empty()


# ── match_fills_to_level_changes ─────────────────────────────────────────────


def _make_level_changes(
    token_id: str,
    price_str: str,  # raw price from pm_clob (variable dp)
    t_new_order: int,
    t_cancel_or_fill: int,
) -> pl.DataFrame:
    """Create a minimal level_changes DataFrame for testing.

    price_str is stored as given (NOT pre-normalized), to test that
    the normalization in _normalize_price is applied at _explode time.
    Here we pre-normalize manually to simulate what build_level_changes produces.
    """
    norm_price = _normalize_price(price_str)
    return pl.DataFrame(
        {
            "token_id": [token_id, token_id],
            "price": [norm_price, norm_price],
            "side": ["SELL", "SELL"],
            "t_recv_ns": [t_new_order, t_cancel_or_fill],
            "size_before": [0.0, 100.0],
            "size_after": [100.0, 0.0],
            "delta": [100.0, -100.0],
            "classification": ["new_order", "cancel_or_fill"],
        }
    )


def _make_fills(
    token_id: str,
    fill_price: str,  # 6dp string as stored in ohanism_fills
    t_ws_ns: int,
    block_number: int = 100,
    log_index: int = 0,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "block_number": [block_number],
            "log_index": [log_index],
            "token_id": [token_id],
            "price": [fill_price],
            "size": ["100.000000"],
            "t_ws_ns": [t_ws_ns],
        }
    )


class TestMatchFillsToLevelChanges:
    def test_matches_when_prices_differ_in_dp(self) -> None:
        """Regression: pm_clob price "0.61" must match fill price "0.610000"."""
        t_new = 1_000_000_000
        t_fill = 3_000_000_000
        lc = _make_level_changes("TOKEN1", "0.61", t_new, t_fill)
        fills = _make_fills("TOKEN1", "0.610000", t_fill)
        result = match_fills_to_level_changes(lc, fills, tolerance_s=5.0)
        assert "quote_arrival_ns" in result.columns
        # Should find the new_order before the fill
        assert result["quote_arrival_ns"][0] == t_new
        lifetime = result["quote_lifetime_ms"][0]
        assert lifetime is not None
        assert abs(lifetime - 2000.0) < 1.0  # (t_fill - t_new) / 1e6

    def test_no_match_outside_tolerance(self) -> None:
        t_new = 1_000_000_000
        t_cancel = 2_000_000_000
        t_fill = 100_000_000_000  # 100s later — outside 5s tolerance
        lc = _make_level_changes("TOKEN1", "0.61", t_new, t_cancel)
        fills = _make_fills("TOKEN1", "0.610000", t_fill)
        result = match_fills_to_level_changes(lc, fills, tolerance_s=5.0)
        assert result["quote_arrival_ns"][0] is None
        assert result["quote_lifetime_ms"][0] is None

    def test_different_token_no_match(self) -> None:
        lc = _make_level_changes("TOKEN1", "0.61", 1_000_000_000, 3_000_000_000)
        fills = _make_fills("TOKEN2", "0.610000", 3_000_000_000)
        result = match_fills_to_level_changes(lc, fills, tolerance_s=5.0)
        assert result["quote_arrival_ns"][0] is None

    def test_five_dp_price(self) -> None:
        """pm_clob price "0.01000" (5dp) must match fill "0.010000" (6dp)."""
        lc = _make_level_changes("T1", "0.01000", 1_000_000_000, 2_000_000_000)
        fills = _make_fills("T1", "0.010000", 2_000_000_000)
        result = match_fills_to_level_changes(lc, fills, tolerance_s=5.0)
        assert result["quote_arrival_ns"][0] == 1_000_000_000

    def test_integer_price_match(self) -> None:
        """pm_clob price "1" must match fill "1.000000"."""
        lc = _make_level_changes("T1", "1", 1_000_000_000, 2_000_000_000)
        fills = _make_fills("T1", "1.000000", 2_000_000_000)
        result = match_fills_to_level_changes(lc, fills, tolerance_s=5.0)
        assert result["quote_arrival_ns"][0] == 1_000_000_000

    def test_multiple_fills_match_independently(self) -> None:
        """Two fills at different prices both match their respective levels."""
        lc = pl.concat(
            [
                _make_level_changes("T1", "0.50", 1_000_000_000, 2_000_000_000),
                _make_level_changes("T1", "0.60", 1_500_000_000, 3_000_000_000),
            ]
        )
        fills_50 = _make_fills("T1", "0.500000", 2_000_000_000, block_number=1)
        fills_60 = _make_fills("T1", "0.600000", 3_000_000_000, block_number=2)
        fills = pl.concat([fills_50, fills_60])
        result = match_fills_to_level_changes(lc, fills, tolerance_s=5.0)
        arrivals = result.sort("block_number")["quote_arrival_ns"].to_list()
        assert arrivals[0] == 1_000_000_000  # 0.50 level order
        assert arrivals[1] == 1_500_000_000  # 0.60 level order
