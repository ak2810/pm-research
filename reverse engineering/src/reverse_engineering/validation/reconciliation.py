"""Phase 1: Fill count and PnL reconciliation against ohanism's public profile.

Compares fills extracted from local Parquet cache against
data-api.polymarket.com/activity for a fixed 24h window.

Acceptance gates:
- Fill count within ±0.5% of API count
- Realized PnL within ±0.1% of API PnL
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import polars as pl
import requests
import structlog

log = structlog.get_logger(__name__)

_API_BASE = "https://data-api.polymarket.com"
_OHANISM_PROXY = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
_PAGE_SIZE = 500


def fetch_api_trades(
    wallet: str,
    window_start_unix: float,
    window_end_unix: float,
    *,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Fetch TRADE-type activity from data-api for a fixed time window.

    Paginates /activity in descending order until trades are older than window_start.

    Args:
        wallet: 0x proxy wallet address.
        window_start_unix: Window start (Unix seconds, inclusive).
        window_end_unix: Window end (Unix seconds, inclusive).
        timeout: HTTP request timeout in seconds.

    Returns:
        List of TRADE activity dicts within the window.
    """
    trades: list[dict[str, Any]] = []
    offset = 0

    while True:
        url = f"{_API_BASE}/activity?user={wallet}&limit={_PAGE_SIZE}&offset={offset}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        batch: list[dict[str, Any]] = resp.json()

        if not batch:
            break

        for item in batch:
            ts = item.get("timestamp", 0)
            if isinstance(ts, int | float) and ts < window_start_unix:
                log.info(
                    "api_fetch_past_window",
                    ts=ts,
                    window_start=window_start_unix,
                    total_collected=len(trades),
                )
                return trades
            if (
                item.get("type") == "TRADE"
                and isinstance(ts, int | float)
                and window_start_unix <= ts <= window_end_unix
            ):
                trades.append(item)

        offset += _PAGE_SIZE
        log.debug("api_page_fetched", offset=offset, collected=len(trades))

    return trades


def compute_local_pnl(fills: pl.DataFrame) -> Decimal:
    """Compute realized PnL from ohanism_fills DataFrame.

    PnL = USDC inflows (SELL fills) - USDC outflows (BUY fills) + rebates.

    For ohanism as maker:
    - ohanism_side=SELL: ohanism received USDC (inflow) = price * size
    - ohanism_side=BUY: ohanism paid USDC (outflow) = price * size
    Both price and size are 6dp string Decimal columns.
    """
    pnl = Decimal("0")
    for row in fills.iter_rows(named=True):
        price = Decimal(row["price"])
        size = Decimal(row["size"])
        rebate = Decimal(row["rebate_received"])
        notional = price * size
        if row["ohanism_side"] == "SELL":
            pnl += notional
        else:
            pnl -= notional
        pnl += rebate
    return pnl


def reconcile(
    fills: pl.DataFrame,
    window_start_ns: int,
    window_end_ns: int,
) -> dict[str, Any]:
    """Run fill count and PnL reconciliation for a fixed time window.

    Args:
        fills: ohanism_fills DataFrame (all columns).
        window_start_ns: Window start in nanoseconds (t_block_ns basis).
        window_end_ns: Window end in nanoseconds.

    Returns:
        Dict with reconciliation results including pass/fail for each gate.
    """
    window_start_s = window_start_ns / 1e9
    window_end_s = window_end_ns / 1e9

    window_fills = fills.filter(
        (pl.col("t_block_ns") >= window_start_ns) & (pl.col("t_block_ns") <= window_end_ns)
    )
    local_count = len(window_fills)
    local_pnl = compute_local_pnl(window_fills)

    log.info(
        "reconcile_local",
        local_count=local_count,
        local_pnl=str(local_pnl),
        window_start_s=window_start_s,
        window_end_s=window_end_s,
    )

    api_trades = fetch_api_trades(_OHANISM_PROXY, window_start_s, window_end_s)
    api_count = len(api_trades)

    # API PnL: data-api side = TAKER's side. ohanism is maker.
    # side='BUY' (taker bought from ohanism) → ohanism sold tokens, received USDC → +
    # side='SELL' (taker sold to ohanism) → ohanism bought tokens, paid USDC → -
    api_pnl = Decimal("0")
    for t in api_trades:
        usdc_size = Decimal(str(t.get("usdcSize", 0) or 0))
        side = t.get("side", "")
        if side == "BUY":
            api_pnl += usdc_size
        elif side == "SELL":
            api_pnl -= usdc_size

    count_gap_pct = abs(local_count - api_count) / max(api_count, 1) * 100
    pnl_gap_pct = abs(float(local_pnl - api_pnl)) / max(abs(float(api_pnl)), 1e-6) * 100

    result: dict[str, Any] = {
        "local_count": local_count,
        "api_count": api_count,
        "count_gap_pct": round(count_gap_pct, 3),
        "count_gate_pass": count_gap_pct <= 0.5,
        "local_pnl": str(local_pnl),
        "api_pnl": str(api_pnl),
        "pnl_gap_pct": round(pnl_gap_pct, 3),
        "pnl_gate_pass": pnl_gap_pct <= 0.1,
        "window_start_s": window_start_s,
        "window_end_s": window_end_s,
    }

    log.info("reconcile_result", **{k: str(v) for k, v in result.items()})
    return result


def verify_sign_discipline(
    fills: pl.DataFrame,
    ltp_df: pl.DataFrame,
    sample_n: int = 100,
) -> dict[str, Any]:
    """Verify ohanism_side mapping via pm_clob last_trade_price side field.

    Joins fills to pm_clob last_trade_price by tx_hash. Checks that:
    - polygon side=0 (taker BUY) → pm_clob side='BUY' (same taker direction)
    - polygon side=1 (taker SELL) → pm_clob side='SELL'

    Args:
        fills: ohanism_fills with tx_hash and side columns.
        ltp_df: pm_clob last_trade_price rows with transaction_hash and side.
        sample_n: Number of fills to sample for verification.

    Returns:
        Dict with agreement_rate and interpretation verdict.
    """
    if "transaction_hash" not in ltp_df.columns:
        return {"error": "ltp_df missing transaction_hash column"}

    sample = fills.filter(pl.col("t_ws_method") == "tx_hash").head(sample_n)
    if len(sample) == 0:
        return {"error": "No tx_hash-matched fills to verify"}

    joined = sample.join(
        ltp_df.select(["transaction_hash", "side"]).rename(
            {"side": "pmclob_side", "transaction_hash": "tx_hash_ltp"}
        ),
        left_on="tx_hash",
        right_on="tx_hash_ltp",
        how="inner",
    )

    if len(joined) == 0:
        return {"error": "No matching rows between fills and ltp_df on tx_hash"}

    poly_side = joined["side"]
    pmclob_side = joined["pmclob_side"]

    agree_count = (
        ((poly_side == 0) & (pmclob_side == "BUY")) | ((poly_side == 1) & (pmclob_side == "SELL"))
    ).sum()

    agree_rate = float(agree_count) / len(joined)

    interpretation = (
        "CONFIRMED: side=0→taker_BUY→ohanism_SELL; side=1→taker_SELL→ohanism_BUY"
        if agree_rate > 0.95
        else f"AMBIGUOUS: agreement_rate={agree_rate:.3f}"
    )

    return {
        "sample_size": len(joined),
        "agree_count": int(agree_count),
        "agreement_rate": round(agree_rate, 4),
        "interpretation": interpretation,
    }
