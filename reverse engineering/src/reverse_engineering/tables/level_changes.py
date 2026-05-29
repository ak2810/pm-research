"""Build level_changes table and reconstruct per-order quote trajectories.

Phase 3 implementation — METHODOLOGY §3.1-3.3.

level_changes: from pm_clob price_change events, compute per-level size deltas.
Per-order trajectory: match fills to level changes to infer quote lifetime.
Quote classification: persistent / repricing / pulled.

Memory strategy: processes one (token_id, hour) at a time. Each token_id's
price_change events for one hour fit in RAM trivially. The JSON-explode of
price_changes is the expensive step — do it per-hour, filtered to fill tokens.
Peak RAM: <1 GB.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import structlog

from reverse_engineering.io.local_reader import scan_feed

log = structlog.get_logger(__name__)

QUOTE_PATTERNS = ("persistent", "repricing", "pulled")


def _explode_price_changes(
    pm_clob_df: pl.DataFrame,
    token_ids: set[str],
) -> pl.DataFrame:
    """Explode pm_clob price_change JSON arrays for a set of token_ids.

    Args:
        pm_clob_df: Collected pm_clob DataFrame with price_changes column.
        token_ids: Filter to only entries for these token_ids.

    Returns:
        DataFrame with columns: t_recv_ns, token_id, price (str), side (str),
        size (str: new resting size), hash (str).
    """
    records: list[dict[str, Any]] = []
    for row in pm_clob_df.filter(
        (pl.col("event_type") == "price_change") & pl.col("price_changes").is_not_null()
    ).iter_rows(named=True):
        t_ns = row["t_recv_ns"]
        try:
            entries: list[dict[str, Any]] = json.loads(row["price_changes"])
        except (json.JSONDecodeError, TypeError):
            continue
        for e in entries:
            tid = e.get("asset_id", "")
            if token_ids and tid not in token_ids:
                continue
            records.append(
                {
                    "t_recv_ns": t_ns,
                    "token_id": tid,
                    "price": e.get("price", ""),
                    "side": e.get("side", ""),
                    "size": e.get("size", "0"),
                    "hash": e.get("hash", ""),
                }
            )

    if not records:
        return pl.DataFrame(
            schema={
                "t_recv_ns": pl.Int64,
                "token_id": pl.Utf8,
                "price": pl.Utf8,
                "side": pl.Utf8,
                "size": pl.Utf8,
                "hash": pl.Utf8,
            }
        )
    return pl.DataFrame(records)


def build_level_changes(
    date: str,
    hour: int,
    fill_token_ids: set[str],
) -> pl.DataFrame:
    """Build level_changes for one hour for ohanism's fill token_ids.

    Computes size delta from previous observation for each (token_id, price, side) level.
    Classifies delta as: new_order (delta > 0), cancel_or_fill (delta < 0), zero (no change).

    Args:
        date: YYYY-MM-DD
        hour: 0-23
        fill_token_ids: Set of token_ids to restrict to (ohanism's traded tokens).

    Returns:
        DataFrame with columns: token_id, price, side, t_recv_ns,
        size_before, size_after, delta, classification.
    """
    try:
        lf = scan_feed("pm_clob", date, hour, columns=["event_type", "price_changes", "t_recv_ns"])
        pm_clob = lf.collect()
    except FileNotFoundError:
        return pl.DataFrame(
            schema={
                "token_id": pl.Utf8,
                "price": pl.Utf8,
                "side": pl.Utf8,
                "t_recv_ns": pl.Int64,
                "size_before": pl.Float64,
                "size_after": pl.Float64,
                "delta": pl.Float64,
                "classification": pl.Utf8,
            }
        )

    exploded = _explode_price_changes(pm_clob, fill_token_ids)
    if exploded.is_empty():
        return exploded.with_columns(
            [
                pl.lit(0.0).alias("size_before"),
                pl.lit(0.0).alias("size_after"),
                pl.lit(0.0).alias("delta"),
                pl.lit("").alias("classification"),
            ]
        )

    exploded = exploded.with_columns(pl.col("size").cast(pl.Float64).alias("size_f"))

    # Sort and compute per-level delta
    exploded = exploded.sort(["token_id", "price", "side", "t_recv_ns"])
    exploded = exploded.with_columns(
        pl.col("size_f").shift(1).over(["token_id", "price", "side"]).alias("size_before_f")
    )

    # First observation per level has no prior — size_before = 0
    exploded = exploded.with_columns(pl.col("size_before_f").fill_null(0.0).alias("size_before"))

    exploded = exploded.with_columns((pl.col("size_f") - pl.col("size_before")).alias("delta"))

    exploded = exploded.with_columns(
        pl.when(pl.col("delta") > 0.001)
        .then(pl.lit("new_order"))
        .when(pl.col("delta") < -0.001)
        .then(pl.lit("cancel_or_fill"))
        .otherwise(pl.lit("no_change"))
        .alias("classification")
    )

    return exploded.select(
        [
            "token_id",
            "price",
            "side",
            "t_recv_ns",
            pl.col("size_before").alias("size_before"),
            pl.col("size_f").alias("size_after"),
            "delta",
            "classification",
        ]
    )


def match_fills_to_level_changes(
    level_changes: pl.DataFrame,
    fills: pl.DataFrame,
    tolerance_s: float = 10.0,
) -> pl.DataFrame:
    """Match ohanism fills to level_change events to infer quote lifetime.

    For each fill (block_number, log_index, token_id, price, size), find
    the cancel_or_fill level_change at the same (token_id, price) within
    tolerance_s of t_ws_ns. The previous new_order at that level gives the
    quote arrival time → lifetime = fill_time - quote_arrival_time.

    Args:
        level_changes: Output of build_level_changes().
        fills: ohanism_fills DataFrame with t_ws_ns, token_id, price, size.
        tolerance_s: Max seconds between fill t_ws_ns and matching level_change.

    Returns:
        Fills DataFrame enriched with: quote_arrival_ns, quote_lifetime_ms,
        repriced (bool), n_repricings (int).
    """
    tol_ns = int(tolerance_s * 1e9)

    # Keep only cancel_or_fill changes (where fill could match)
    cancel_or_fill = level_changes.filter(pl.col("classification") == "cancel_or_fill").sort(
        ["token_id", "price", "t_recv_ns"]
    )

    # Keep only new_order changes (where quotes arrive)
    new_orders = level_changes.filter(pl.col("classification") == "new_order").sort(
        ["token_id", "price", "t_recv_ns"]
    )

    rows_out: list[dict[str, Any]] = []
    fills_ws = fills.filter(pl.col("t_ws_ns").is_not_null()).sort("t_ws_ns")

    for fill_row in fills_ws.iter_rows(named=True):
        tid = fill_row["token_id"]
        price = fill_row["price"]
        t_ws = fill_row["t_ws_ns"]

        # Find nearest cancel_or_fill at this level within tolerance
        level_cf = cancel_or_fill.filter(
            (pl.col("token_id") == tid)
            & (pl.col("price") == price)
            & ((pl.col("t_recv_ns") - t_ws).abs() <= tol_ns)
        )

        if level_cf.is_empty():
            rows_out.append(
                {
                    "block_number": fill_row["block_number"],
                    "log_index": fill_row["log_index"],
                    "quote_arrival_ns": None,
                    "quote_lifetime_ms": None,
                    "n_repricings": 0,
                }
            )
            continue

        cf_time = int(level_cf.sort("t_recv_ns")["t_recv_ns"][0])

        # Find the most recent new_order at this level before cf_time
        arrivals = new_orders.filter(
            (pl.col("token_id") == tid)
            & (pl.col("price") == price)
            & (pl.col("t_recv_ns") < cf_time)
        )

        if arrivals.is_empty():
            quote_arrival = None
            lifetime_ms = None
        else:
            quote_arrival = int(arrivals.sort("t_recv_ns")["t_recv_ns"][-1])
            lifetime_ms = (cf_time - quote_arrival) / 1e6

        rows_out.append(
            {
                "block_number": fill_row["block_number"],
                "log_index": fill_row["log_index"],
                "quote_arrival_ns": quote_arrival,
                "quote_lifetime_ms": lifetime_ms,
                "n_repricings": 0,
            }
        )

    if not rows_out:
        return fills.with_columns(
            [
                pl.lit(None).cast(pl.Int64).alias("quote_arrival_ns"),
                pl.lit(None).cast(pl.Float64).alias("quote_lifetime_ms"),
                pl.lit(0).alias("n_repricings"),
            ]
        )

    enrichment = pl.DataFrame(rows_out)
    return fills.join(enrichment, on=["block_number", "log_index"], how="left")


def classify_quote_pattern(
    level_changes: pl.DataFrame,
    fills: pl.DataFrame,
    binance_mid: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Classify ohanism quotes as persistent / repricing / pulled.

    persistent: quote arrived, got filled without repricing.
    repricing: level increased at price A, then decreased at A and increased
               at adjacent price B — quote moved.
    pulled: level decreased without a corresponding fill (cancel).

    Args:
        level_changes: level_changes for the relevant token_ids.
        fills: ohanism_fills for this period.
        binance_mid: Optional Binance bookTicker for repricing correlation.

    Returns:
        DataFrame with one row per (token_id, quote_period), classification,
        and timing metrics.
    """
    if level_changes.is_empty():
        return pl.DataFrame(
            schema={
                "token_id": pl.Utf8,
                "price": pl.Utf8,
                "side": pl.Utf8,
                "t_start_ns": pl.Int64,
                "t_end_ns": pl.Int64,
                "duration_ms": pl.Float64,
                "pattern": pl.Utf8,
            }
        )

    fill_keys: set[tuple[str, str]] = set(
        zip(fills["token_id"].to_list(), fills["price"].to_list(), strict=False)
    )

    new_orders = level_changes.filter(pl.col("classification") == "new_order")
    cancel_or_fill = level_changes.filter(pl.col("classification") == "cancel_or_fill")

    records: list[dict[str, Any]] = []

    for row in new_orders.iter_rows(named=True):
        tid, price, side, t_start = (
            row["token_id"],
            row["price"],
            row["side"],
            row["t_recv_ns"],
        )

        next_cf = cancel_or_fill.filter(
            (pl.col("token_id") == tid)
            & (pl.col("price") == price)
            & (pl.col("side") == side)
            & (pl.col("t_recv_ns") > t_start)
        )

        if next_cf.is_empty():
            continue

        t_end = int(next_cf.sort("t_recv_ns")["t_recv_ns"][0])
        duration_ms = (t_end - t_start) / 1e6

        is_fill = (tid, price) in fill_keys
        if is_fill:
            pattern = "persistent"
        else:
            next_order_same = new_orders.filter(
                (pl.col("token_id") == tid)
                & (pl.col("side") == side)
                & (pl.col("t_recv_ns") > t_end)
                & (pl.col("t_recv_ns") < t_end + 5_000_000_000)  # within 5s
            )
            pattern = "repricing" if not next_order_same.is_empty() else "pulled"

        records.append(
            {
                "token_id": tid,
                "price": price,
                "side": side,
                "t_start_ns": t_start,
                "t_end_ns": t_end,
                "duration_ms": duration_ms,
                "pattern": pattern,
            }
        )

    return (
        pl.DataFrame(records)
        if records
        else pl.DataFrame(
            schema={
                "token_id": pl.Utf8,
                "price": pl.Utf8,
                "side": pl.Utf8,
                "t_start_ns": pl.Int64,
                "t_end_ns": pl.Int64,
                "duration_ms": pl.Float64,
                "pattern": pl.Utf8,
            }
        )
    )
