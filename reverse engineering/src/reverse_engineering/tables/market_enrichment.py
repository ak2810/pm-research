"""Build full market metadata lookup for ohanism fills.

Two-stage process:
1. token_id → condition_id: from pm_clob book events (covers ~82% of fill tokens).
2. condition_id → market metadata: Gamma API (slug, dates, token_ids).
3. start_strike_price: Binance bookTicker mid at market startDate.

Coverage: 1352/1651 (~82%) ohanism fill token_ids; 299 remain null (from
short-lived 5m markets the pm_clob collector never subscribed to).

Gotcha #17: 5m/15m markets resolve on Chainlink; hourly on Binance. We only
record Binance. For 5m/15m, Binance spot at startDate is a PROXY for the
Chainlink strike. The Chainlink↔Binance basis is a known residual in Phase 4.

Memory strategy: book event scan reads only asset_id+market columns; Binance
spot lookup uses one lazy join per symbol. Peak RAM: <500 MB.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import structlog

from reverse_engineering.config import get_settings
from reverse_engineering.io.gamma import build_market_lookup_from_cids
from reverse_engineering.io.local_reader import scan_feed

log = structlog.get_logger(__name__)

_SYMBOL_STREAM: dict[str, str] = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
}


def build_tid_to_cid(fill_token_ids: set[str]) -> dict[str, str]:
    """Build token_id → condition_id from pm_clob book events.

    Scans all locally cached pm_clob partitions for book events and extracts
    the (asset_id, market) pair. Only keeps entries for tokens in fill_token_ids.

    Args:
        fill_token_ids: Set of token_ids present in ohanism fills.

    Returns:
        Dict {token_id: condition_id} for however many tokens are found.
    """
    cfg = get_settings()
    tid2cid: dict[str, str] = {}

    for parquet in sorted(cfg.cache_dir.glob("feed=pm_clob/date=*/hour=*/data.parquet")):
        if not fill_token_ids - set(tid2cid):
            break
        lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False)
        df = (
            lf.filter(
                (pl.col("event_type") == "book")
                & pl.col("asset_id").is_not_null()
                & pl.col("market").is_not_null()
            )
            .select(["asset_id", "market"])
            .collect()
        )
        for row in df.iter_rows(named=True):
            tid = row["asset_id"]
            if tid in fill_token_ids and tid not in tid2cid:
                tid2cid[tid] = row["market"]

    log.info(
        "tid2cid_built",
        resolved=len(tid2cid),
        missing=len(fill_token_ids) - len(tid2cid),
    )
    return tid2cid


def build_start_strike_prices(
    market_lookup: pl.DataFrame,
    dates: list[str],
) -> pl.DataFrame:
    """Add start_strike_price column to market_lookup using Binance bookTicker.

    Finds the nearest Binance bookTicker mid to each market's startDate.
    Returns market_lookup with start_strike_price (Utf8, 6dp string) added.

    Gotcha #17: Binance price is a proxy for the Chainlink strike on 5m/15m
    markets. The basis is a known residual — noted but not corrected here.

    Args:
        market_lookup: DataFrame with token_id, market, asset_symbol, horizon,
            outcome_side, start_date_unix, end_date_unix.
        dates: List of YYYY-MM-DD dates with local Binance data.

    Returns:
        market_lookup with start_strike_price column (Utf8, null if unavailable).
    """
    if market_lookup.is_empty():
        return market_lookup.with_columns(pl.lit(None).cast(pl.Utf8).alias("start_strike_price"))

    # Build Binance bookTicker DataFrame per symbol
    # (bid+ask)/2 = mid price; closest t_recv_ns to startDate*1e9
    unique_markets = market_lookup.unique(subset=["market"]).select(
        ["market", "asset_symbol", "start_date_unix"]
    )

    strike_rows: list[dict[str, Any]] = []

    for sym, stream_base in _SYMBOL_STREAM.items():
        sym_markets = unique_markets.filter(pl.col("asset_symbol") == sym)
        if sym_markets.is_empty():
            continue

        start_ns_list = (sym_markets["start_date_unix"] * 1e9).cast(pl.Int64).to_list()
        min_ns = min(start_ns_list) - 60_000_000_000  # 60s before earliest start
        max_ns = max(start_ns_list) + 60_000_000_000  # 60s after latest start

        ticker_frames: list[pl.DataFrame] = []
        for date in dates:
            try:
                lf = scan_feed(
                    "binance",
                    date,
                    columns=["e", "s", "b", "a", "t_recv_ns"],
                )
                df = lf.filter(
                    (pl.col("e") == "bookTicker")
                    & (pl.col("s").str.to_lowercase() == f"{stream_base}usdt")
                    & (pl.col("t_recv_ns") >= min_ns)
                    & (pl.col("t_recv_ns") <= max_ns)
                ).collect()
                if len(df) > 0:
                    ticker_frames.append(df)
            except FileNotFoundError:
                continue

        if not ticker_frames:
            log.warning("no_binance_ticker_for_symbol", symbol=sym)
            continue

        ticker = (
            pl.concat(ticker_frames)
            .with_columns(
                ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
            )
            .sort("t_recv_ns")
        )

        # For each market with this symbol, find closest bookTicker
        markets_df = sym_markets.with_columns(
            (pl.col("start_date_unix") * 1e9).cast(pl.Int64).alias("start_ns")
        ).sort("start_ns")

        joined = markets_df.join_asof(
            ticker.select(["t_recv_ns", "mid"]),
            left_on="start_ns",
            right_on="t_recv_ns",
            strategy="nearest",
        )

        for row in joined.iter_rows(named=True):
            mid = row.get("mid")
            if mid is not None:
                from decimal import Decimal

                strike_str = str(Decimal(str(mid)).quantize(Decimal("0.000001")))
                strike_rows.append({"market": row["market"], "start_strike_price": strike_str})

    if not strike_rows:
        return market_lookup.with_columns(pl.lit(None).cast(pl.Utf8).alias("start_strike_price"))

    strikes_df = pl.DataFrame(strike_rows).unique(subset=["market"])
    return market_lookup.join(strikes_df, on="market", how="left")


def build_full_market_lookup(
    fill_token_ids: set[str],
    dates: list[str],
) -> pl.DataFrame:
    """Build complete market lookup: token_id → all metadata + start_strike_price.

    Args:
        fill_token_ids: All unique token_ids in ohanism fills.
        dates: Dates with local data (for Binance lookups).

    Returns:
        DataFrame with: token_id, market, asset_symbol, horizon, outcome_side,
        start_date_unix, end_date_unix, start_strike_price.
    """
    # Stage 1: token_id → condition_id from pm_clob book events
    tid2cid = build_tid_to_cid(fill_token_ids)

    # Stage 2: condition_id → market metadata from Gamma
    unique_cids = list(set(tid2cid.values()))
    log.info("querying_gamma", unique_cids=len(unique_cids))
    market_lookup = build_market_lookup_from_cids(unique_cids)

    # Stage 3: filter to tokens in our fill set (Gamma may return partner token too)
    market_lookup = market_lookup.filter(pl.col("token_id").is_in(list(fill_token_ids)))
    log.info("market_lookup_from_gamma", rows=len(market_lookup))

    # Stage 4: start_strike_price from Binance bookTicker
    return build_start_strike_prices(market_lookup, dates)
