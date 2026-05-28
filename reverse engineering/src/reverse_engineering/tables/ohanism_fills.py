"""Build the ohanism_fills table from polygon + pm_clob + pm_meta local cache.

Phase 2 implementation — called from the Phase 1 notebook to produce
output/tables/ohanism_fills.parquet.

Price formula (empirically verified, notes/VERIFIED_FACTS_RE.md):
- side=0 (taker BUY, ohanism SELL): price = maker_amount_decimal / taker_amount_decimal
- side=1 (taker SELL, ohanism BUY): price = taker_amount_decimal / maker_amount_decimal

All money columns stored as 6-dp Decimal strings. Timestamps as Int64 ns.

Memory strategy: processes one hour at a time. Polygon fill slice is
~100-1,000 rows/hour (all ohanism fills). pm_clob last_trade_price join and
price_change fallback operate on token-filtered subsets. Peak RAM: <500 MB.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

import polars as pl
import structlog

from reverse_engineering.config import get_settings
from reverse_engineering.io.block_times import fetch_block_times
from reverse_engineering.io.local_reader import scan_feed

log = structlog.get_logger(__name__)

OHANISM_PROXY: Final[str] = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
CTF_V2: Final[str] = "0xe111180000d2663c0091e4f400237545b87b996b"
NEG_RISK_V2: Final[str] = "0xe2222d279d744050d28e00520010520000310f59"

_TAKER_FEE_RATE: Final[Decimal] = Decimal("0.07")
_REBATE_RATE: Final[Decimal] = Decimal("0.2")
_SIX_DP = Decimal("0.000001")
_BACKFILL_THRESHOLD_NS: Final[int] = 10_000_000_000  # 10 seconds

# Canonical column order for ohanism_fills.parquet
OHANISM_FILLS_COLUMNS: Final[list[str]] = [
    "block_number",
    "log_index",
    "t_recv_ns",
    "t_block_ns",
    "t_ws_ns",
    "t_ws_method",
    "is_backfilled",
    "tx_hash",
    "order_hash",
    "exchange",
    "token_id",
    "market",
    "asset_symbol",
    "horizon",
    "is_maker",
    "ohanism_side",
    "outcome_side",
    "price",
    "size",
    "fee_paid",
    "rebate_received",
    "time_to_expiry_s",
    "start_strike_price",
    "builder",
    "metadata",
]

_ASSET_PATTERN = re.compile(r"^([a-z]+)-updown-(5m|15m|1h)-")


def _dec6(val: float) -> str:
    """Format float as 6-decimal-place string for Parquet storage."""
    return str(Decimal(str(val)).quantize(_SIX_DP, rounding=ROUND_HALF_UP))


def extract_raw_fills(date: str, hour: int | None = None) -> pl.LazyFrame:
    """Lazily scan polygon feed and return ohanism OrderFilled rows.

    Applies predicate pushdown: event='OrderFilled', exchange in [CTF_V2, NEG_RISK_V2],
    maker or taker == OHANISM_PROXY.

    Args:
        date: YYYY-MM-DD
        hour: If None, scan all hours for this date.

    Returns:
        LazyFrame with full polygon schema, filtered to ohanism fills.
    """
    lf = scan_feed("polygon", date, hour)
    return lf.filter(
        (pl.col("event") == "OrderFilled")
        & (pl.col("exchange").is_in([CTF_V2, NEG_RISK_V2]))
        & ((pl.col("maker") == OHANISM_PROXY) | (pl.col("taker") == OHANISM_PROXY))
    )


def _build_market_lookup(dates: list[str]) -> pl.DataFrame:
    """Build token_id → market metadata lookup from pm_meta.

    Parses market_snapshot events. Skips negRisk markets.
    Handles double-encoded clobTokenIds.

    Args:
        dates: List of YYYY-MM-DD dates to scan.

    Returns:
        DataFrame with columns: token_id, market, asset_symbol, horizon,
        outcome_side, end_date_unix (float seconds), start_date_unix.
    """
    records: list[dict[str, Any]] = []
    seen_token_ids: set[str] = set()

    for date in dates:
        try:
            lf = scan_feed("pm_meta", date)
        except FileNotFoundError:
            continue

        meta_rows = lf.filter(
            (pl.col("event_type") == "market_snapshot") & pl.col("market").is_not_null()
        ).collect()

        for row in meta_rows.iter_rows(named=True):
            try:
                mkt: dict[str, Any] = json.loads(row["market"])
            except (json.JSONDecodeError, TypeError):
                continue

            if mkt.get("negRisk"):
                continue

            try:
                token_ids: list[str] = json.loads(mkt["clobTokenIds"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

            slug = mkt.get("slug", "")
            m = _ASSET_PATTERN.match(slug)
            if not m:
                continue

            asset_symbol = m.group(1).upper()
            horizon = m.group(2)
            condition_id: str = mkt.get("conditionId", "")

            end_date_str: str = mkt.get("endDate", "")
            start_date_str: str = mkt.get("startDate", "")

            try:
                end_unix = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).timestamp()
                start_unix = datetime.fromisoformat(
                    start_date_str.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                continue

            for i, tok_id in enumerate(token_ids):
                if tok_id in seen_token_ids:
                    continue
                seen_token_ids.add(tok_id)
                outcome_side = "Up" if i == 0 else "Down"
                records.append(
                    {
                        "token_id": tok_id,
                        "market": condition_id,
                        "asset_symbol": asset_symbol,
                        "horizon": horizon,
                        "outcome_side": outcome_side,
                        "end_date_unix": end_unix,
                        "start_date_unix": start_unix,
                    }
                )

    if not records:
        return pl.DataFrame(
            schema={
                "token_id": pl.Utf8,
                "market": pl.Utf8,
                "asset_symbol": pl.Utf8,
                "horizon": pl.Utf8,
                "outcome_side": pl.Utf8,
                "end_date_unix": pl.Float64,
                "start_date_unix": pl.Float64,
            }
        )

    return pl.DataFrame(records).unique(subset=["token_id"])


def _build_ltp_lookup(dates: list[str]) -> pl.DataFrame:
    """Build tx_hash → t_ws_ns lookup from pm_clob last_trade_price events.

    Deduplicates by tx_hash keeping the earliest t_recv_ns.

    Args:
        dates: List of YYYY-MM-DD dates to scan.

    Returns:
        DataFrame with columns: transaction_hash, t_ws_ns.
    """
    frames: list[pl.DataFrame] = []
    for date in dates:
        try:
            lf = scan_feed(
                "pm_clob",
                date,
                columns=["event_type", "transaction_hash", "t_recv_ns"],
            )
        except FileNotFoundError:
            continue

        df = lf.filter(
            (pl.col("event_type") == "last_trade_price") & pl.col("transaction_hash").is_not_null()
        ).collect()
        if len(df) > 0:
            frames.append(df)

    if not frames:
        return pl.DataFrame(schema={"transaction_hash": pl.Utf8, "t_ws_ns": pl.Int64})

    combined = pl.concat(frames)
    return (
        combined.sort("t_recv_ns")
        .unique(subset=["transaction_hash"], keep="first")
        .select(
            [
                pl.col("transaction_hash"),
                pl.col("t_recv_ns").alias("t_ws_ns"),
            ]
        )
    )


def build_ohanism_fills(
    dates: list[str],
) -> pl.DataFrame:
    """Build the complete ohanism_fills table for the given dates.

    Processes one hour at a time (memory discipline). Enriches with:
    - t_block_ns from RPC batch fetch + cache
    - t_ws_ns from pm_clob last_trade_price (tx_hash join, ~90% match)
    - Market metadata from pm_meta (asset_symbol, horizon, outcome_side, TTE)
    - Computed price, size, fee, rebate

    Args:
        dates: List of YYYY-MM-DD dates to process.

    Returns:
        DataFrame with schema matching OHANISM_FILLS_COLUMNS.

    Raises:
        RuntimeError: If POLYGON_HTTPS_URL not configured.
    """
    from reverse_engineering.io.catalog import list_local_partitions

    log.info("build_ohanism_fills_start", dates=dates)

    market_lookup = _build_market_lookup(dates)
    log.info("market_lookup_built", tokens=len(market_lookup))

    ltp_lookup = _build_ltp_lookup(dates)
    log.info("ltp_lookup_built", rows=len(ltp_lookup))

    date_set = set(dates)
    partitions = [p for p in list_local_partitions("polygon") if p.date in date_set]

    # Pre-collect all ohanism fills to find all unique block numbers upfront,
    # then fetch block times in one go (avoids repeated RPC bursts per hour).
    log.info("prefetch_fills_for_block_numbers", partitions=len(partitions))
    raw_all_list: list[pl.DataFrame] = []
    for partition in sorted(partitions, key=lambda p: (p.date, p.hour)):
        df = extract_raw_fills(partition.date, partition.hour).collect()
        if len(df) > 0:
            raw_all_list.append(df)

    if not raw_all_list:
        return pl.DataFrame(schema={c: pl.Utf8 for c in OHANISM_FILLS_COLUMNS})

    all_block_numbers = (
        pl.concat([df.select("block_number") for df in raw_all_list])["block_number"]
        .unique()
        .to_list()
    )
    log.info("fetching_all_block_times", distinct_blocks=len(all_block_numbers))
    bt_map = fetch_block_times([int(b) for b in all_block_numbers])
    bt_df = pl.DataFrame(
        {
            "block_number": pl.Series(list(bt_map.keys()), dtype=pl.Int64),
            "t_block_ns": pl.Series(list(bt_map.values()), dtype=pl.Int64),
        }
    )
    log.info("block_times_ready", fetched=len(bt_map))

    all_fills: list[pl.DataFrame] = []

    for raw in raw_all_list:
        log.info("fills_processing", count=len(raw))

        raw = raw.join(bt_df, on="block_number", how="left")

        # Backfill flag
        raw = raw.with_columns(
            ((pl.col("t_recv_ns") - pl.col("t_block_ns")).abs() > _BACKFILL_THRESHOLD_NS).alias(
                "is_backfilled"
            )
        )

        # t_ws_ns via last_trade_price tx_hash join
        raw = raw.join(
            ltp_lookup.rename({"transaction_hash": "tx_hash_ltp"}),
            left_on="tx_hash",
            right_on="tx_hash_ltp",
            how="left",
        ).rename({"t_ws_ns": "t_ws_ns_ltp"})

        raw = raw.with_columns(
            pl.when(pl.col("t_ws_ns_ltp").is_not_null())
            .then(pl.col("t_ws_ns_ltp"))
            .otherwise(pl.col("t_block_ns"))
            .alias("t_ws_ns"),
            pl.when(pl.col("t_ws_ns_ltp").is_not_null())
            .then(pl.lit("tx_hash"))
            .otherwise(pl.lit("block_approx"))
            .alias("t_ws_method"),
        )

        # is_maker (all observed are maker, but keep generic)
        raw = raw.with_columns((pl.col("maker") == OHANISM_PROXY).alias("is_maker"))

        # ohanism_side: side=0 → taker BUY → ohanism SELL; side=1 → taker SELL → ohanism BUY
        raw = raw.with_columns(
            pl.when(pl.col("side") == 1)
            .then(pl.lit("BUY"))
            .otherwise(pl.lit("SELL"))
            .alias("ohanism_side")
        )

        # Price computation (verified formula)
        raw = raw.with_columns(
            [
                pl.col("maker_amount_decimal").cast(pl.Float64).alias("_ma"),
                pl.col("taker_amount_decimal").cast(pl.Float64).alias("_ta"),
            ]
        )
        raw = raw.with_columns(
            pl.when(pl.col("side") == 0)
            .then(pl.col("_ma") / pl.col("_ta"))
            .otherwise(pl.col("_ta") / pl.col("_ma"))
            .alias("_price_f64"),
            pl.when(pl.col("side") == 0)
            .then(pl.col("_ta"))
            .otherwise(pl.col("_ma"))
            .alias("_size_f64"),
        )

        # Format as 6dp strings
        raw = raw.with_columns(
            [
                pl.col("_price_f64")
                .map_elements(lambda x: _dec6(x), return_dtype=pl.Utf8)
                .alias("price"),
                pl.col("_size_f64")
                .map_elements(lambda x: _dec6(x), return_dtype=pl.Utf8)
                .alias("size"),
                pl.col("fee_decimal").alias("fee_paid"),
            ]
        )

        # Rebate: 0.2 * 0.07 * min(price, 1-price) * size for maker fills
        _fee_factor = float(_REBATE_RATE * _TAKER_FEE_RATE)
        raw = raw.with_columns(
            (
                pl.when(pl.col("is_maker"))
                .then(
                    _fee_factor
                    * pl.min_horizontal(pl.col("_price_f64"), 1.0 - pl.col("_price_f64"))
                    * pl.col("_size_f64")
                )
                .otherwise(pl.lit(0.0))
            ).alias("_rebate_f64")
        )
        raw = raw.with_columns(
            pl.col("_rebate_f64")
            .map_elements(lambda x: _dec6(x), return_dtype=pl.Utf8)
            .alias("rebate_received")
        )

        # Market metadata join
        raw = raw.join(market_lookup, on="token_id", how="left")

        # time_to_expiry_s
        raw = raw.with_columns(
            (pl.col("end_date_unix") - pl.col("t_block_ns") / 1e9).alias("time_to_expiry_s")
        )

        # start_strike_price: null in Phase 1 (filled in Phase 2)
        raw = raw.with_columns(pl.lit(None).cast(pl.Utf8).alias("start_strike_price"))

        # Final column selection
        available = set(raw.columns)
        [c for c in OHANISM_FILLS_COLUMNS if c in available]
        for missing_col in [c for c in OHANISM_FILLS_COLUMNS if c not in available]:
            raw = raw.with_columns(pl.lit(None).cast(pl.Utf8).alias(missing_col))

        all_fills.append(raw.select(OHANISM_FILLS_COLUMNS))

    if not all_fills:
        return pl.DataFrame(schema={c: pl.Utf8 for c in OHANISM_FILLS_COLUMNS})

    result = pl.concat(all_fills).sort(["block_number", "log_index"])
    log.info("build_ohanism_fills_complete", total_rows=len(result))
    return result


def write_ohanism_fills(df: pl.DataFrame) -> None:
    """Write ohanism_fills DataFrame to output/tables/ohanism_fills.parquet."""
    cfg = get_settings()
    out_path = cfg.tables_dir / "ohanism_fills.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(str(out_path), compression="zstd")
    log.info("ohanism_fills_written", path=str(out_path), rows=len(df))
