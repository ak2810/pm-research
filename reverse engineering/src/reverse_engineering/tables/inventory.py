"""Inventory trajectory analysis — pulled forward from Phase 7.1 into Phase 2.

Builds running net position by token_id from ohanism_fills. Computes:
1. Per-market inventory over the market lifecycle.
2. Total dollar exposure across all open markets (sum |pos_i × price_i|).
3. Distribution of peak inventory per market.

All position values in token units (signed: + = long Up, - = short Up).
Dollar exposure uses fill price as the mark; no mid-price interpolation.

Memory strategy: fills are ~21k rows/day — trivially small. All operations
materialize the full fills DataFrame. Peak RAM: <50 MB.
"""

from __future__ import annotations

import polars as pl
import structlog

log = structlog.get_logger(__name__)


def build_inventory_series(fills: pl.DataFrame) -> pl.DataFrame:
    """Build running cumulative inventory by token_id over time.

    Args:
        fills: ohanism_fills DataFrame with columns:
            block_number, log_index, token_id, ohanism_side, size, price,
            t_block_ns.

    Returns:
        DataFrame sorted by (token_id, block_number, log_index) with columns:
        token_id, block_number, log_index, t_block_ns, ohanism_side,
        fill_size (float), signed_size (float), cum_position (float),
        price_float (float), dollar_exposure (float abs(cum_position * price)).
    """
    df = (
        fills.with_columns(
            [
                pl.col("size").cast(pl.Float64).alias("fill_size"),
                pl.col("price").cast(pl.Float64).alias("price_float"),
            ]
        )
        .with_columns(
            pl.when(pl.col("ohanism_side") == "BUY")
            .then(pl.col("fill_size"))
            .otherwise(-pl.col("fill_size"))
            .alias("signed_size")
        )
        .sort(["token_id", "block_number", "log_index"])
        .with_columns(pl.col("signed_size").cum_sum().over("token_id").alias("cum_position"))
        .with_columns(
            (pl.col("cum_position").abs() * pl.col("price_float")).alias("dollar_exposure")
        )
    )
    return df.select(
        [
            "token_id",
            "block_number",
            "log_index",
            "t_block_ns",
            "ohanism_side",
            "fill_size",
            "signed_size",
            "cum_position",
            "price_float",
            "dollar_exposure",
        ]
    )


def compute_peak_inventory_per_market(inv: pl.DataFrame) -> pl.DataFrame:
    """Compute peak absolute inventory and final position per token_id.

    Args:
        inv: Output of build_inventory_series().

    Returns:
        DataFrame with one row per token_id: token_id, fill_count,
        peak_long (max cum_position), peak_short (min cum_position),
        peak_abs (max |cum_position|), final_position (last cum_position),
        peak_dollar_exposure.
    """
    return (
        inv.group_by("token_id")
        .agg(
            [
                pl.len().alias("fill_count"),
                pl.col("cum_position").max().alias("peak_long"),
                pl.col("cum_position").min().alias("peak_short"),
                pl.col("cum_position").abs().max().alias("peak_abs"),
                pl.col("cum_position").last().alias("final_position"),
                pl.col("dollar_exposure").max().alias("peak_dollar_exposure"),
            ]
        )
        .sort("peak_abs", descending=True)
    )


def compute_total_dollar_exposure_series(inv: pl.DataFrame) -> pl.DataFrame:
    """Compute total dollar exposure across all tokens at each fill event.

    Aggregates (block_number, log_index) → sum of |cum_position × price|
    across all tokens with open positions.

    Args:
        inv: Output of build_inventory_series().

    Returns:
        DataFrame sorted by (block_number, log_index) with columns:
        block_number, log_index, t_block_ns, total_dollar_exposure.
    """
    # For each (block_number, log_index), use the CURRENT cum_position for
    # ALL tokens. Since cum_position is computed cumulatively per token, we
    # need the last known position for each token at each point in time.
    # Efficient approach: snapshot per (block_number, log_index) fill.

    (
        inv.select(["block_number", "log_index", "t_block_ns"])
        .unique()
        .sort(["block_number", "log_index"])
    )

    # For each fill event, sum dollar_exposure where the token's last fill
    # is at or before this event
    # Fast approach: at each fill event, include all tokens' current cum_position
    # Simpler: tag each token's cum_position forward to all subsequent fills
    # For ~21k rows, even an O(n²) approach is fast enough
    results = []
    sorted_inv = inv.sort(["block_number", "log_index"])
    # Track running per-token position
    token_pos: dict[str, float] = {}
    token_price: dict[str, float] = {}

    for row in sorted_inv.iter_rows(named=True):
        tid = row["token_id"]
        token_pos[tid] = float(row["cum_position"])
        token_price[tid] = float(row["price_float"])
        total = sum(abs(p) * token_price.get(t, 0.0) for t, p in token_pos.items())
        results.append(
            {
                "block_number": row["block_number"],
                "log_index": row["log_index"],
                "t_block_ns": row["t_block_ns"],
                "total_dollar_exposure": total,
            }
        )

    return pl.DataFrame(results)
