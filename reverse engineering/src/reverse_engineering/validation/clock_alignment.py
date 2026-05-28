"""Phase 1: Cross-feed timestamp alignment checks.

Test 1: polygon t_block_ns vs pm_clob t_ws_ns.
  Acceptance: median Δt < 5s, p99 < 30s.

Test 2: pm_clob t_ws_ns vs nearest Binance aggTrade T (trade time ms).
  Acceptance: median |Δt| < 100ms per symbol.

Backfill flag: polygon rows where |t_recv_ns - t_block_ns| > 10s are backfilled
(t_recv_ns = backfill wall-clock, not block time). See GOTCHAS.md #16.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt
import polars as pl
import structlog

from reverse_engineering.io.local_reader import scan_feed

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

_SYMBOL_TO_STREAM: dict[str, str] = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
}


def check_polygon_pmclob_alignment(
    fills: pl.DataFrame,
    plots_dir: Path,
    sample_n: int = 1000,
) -> dict[str, float]:
    """Test 1: polygon t_block_ns vs pm_clob t_ws_ns.

    Args:
        fills: ohanism_fills DataFrame with t_block_ns and t_ws_ns columns.
        plots_dir: Directory to write clock_polygon_vs_pmclob.png.
        sample_n: Max fills to sample.

    Returns:
        Dict with median_s, p99_s, gate_pass (median<5 and p99<30).
    """
    import matplotlib.pyplot as plt

    sample = fills.filter(
        pl.col("t_block_ns").is_not_null() & pl.col("t_ws_ns").is_not_null()
    ).head(sample_n)

    delta_s: npt.NDArray[np.float64] = (sample["t_ws_ns"] - sample["t_block_ns"]).to_numpy().astype(
        np.float64
    ) / 1e9

    median_s = float(np.median(delta_s))
    p99_s = float(np.percentile(delta_s, 99))

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.hist(delta_s, bins=100, range=(-5, 60))
    ax.axvline(median_s, color="r", linestyle="--", label=f"median={median_s:.2f}s")
    ax.axvline(p99_s, color="orange", linestyle="--", label=f"p99={p99_s:.2f}s")
    ax.set_xlabel("t_ws_ns - t_block_ns (seconds)")
    ax.set_ylabel("count")
    ax.set_title("pm_clob WS timestamp relative to block time")
    ax.legend()
    fig.tight_layout()
    out_path = plots_dir / "clock_polygon_vs_pmclob.png"
    fig.savefig(str(out_path), dpi=150)
    plt.close(fig)

    result = {
        "median_s": round(median_s, 3),
        "p99_s": round(p99_s, 3),
        "sample_n": len(sample),
        "gate_pass": bool(median_s < 5.0 and p99_s < 30.0),
    }
    log.info("clock_test1", **result)
    return result


def check_binance_alignment(
    fills: pl.DataFrame,
    dates: list[str],
    plots_dir: Path,
    sample_n: int = 1000,
) -> dict[str, dict[str, float]]:
    """Test 2: pm_clob t_ws_ns vs nearest Binance aggTrade trade time.

    For each asset symbol, finds the nearest Binance aggTrade by trade
    timestamp T (milliseconds) and computes |t_ws_ms - T| in ms.

    Args:
        fills: ohanism_fills DataFrame with t_ws_ns and asset_symbol.
        dates: Dates to scan for Binance data.
        plots_dir: Directory to write clock_binance_vs_pmclob.png.
        sample_n: Max fills to sample.

    Returns:
        Dict mapping symbol → {median_ms, p99_ms, gate_pass (median<100ms)}.
    """
    import matplotlib.pyplot as plt

    sample = fills.filter(
        pl.col("t_ws_ns").is_not_null() & pl.col("asset_symbol").is_not_null()
    ).head(sample_n)

    sample = sample.with_columns((pl.col("t_ws_ns") // 1_000_000).alias("t_ws_ms").cast(pl.Int64))

    binance_frames: list[pl.DataFrame] = []
    for date in dates:
        try:
            lf = scan_feed(
                "binance",
                date,
                columns=["e", "T", "s", "t_recv_ns"],
            )
            df = lf.filter(pl.col("e") == "aggTrade").collect()
            if len(df) > 0:
                binance_frames.append(df)
        except FileNotFoundError:
            continue

    if not binance_frames:
        log.warning("no_binance_data_for_clock_test")
        return {}

    agg = pl.concat(binance_frames)
    agg = agg.with_columns(
        pl.col("T").cast(pl.Int64).alias("T_ms"),
        pl.col("s").str.to_lowercase().alias("stream_sym"),
    )

    results: dict[str, dict[str, float]] = {}
    fig, axes = plt.subplots(1, len(_SYMBOL_TO_STREAM), figsize=(4 * len(_SYMBOL_TO_STREAM), 4))
    if len(_SYMBOL_TO_STREAM) == 1:
        axes = [axes]

    for ax, (sym, stream_prefix) in zip(axes, _SYMBOL_TO_STREAM.items(), strict=False):
        sym_fills = sample.filter(pl.col("asset_symbol") == sym).sort("t_ws_ms")
        sym_agg = agg.filter(pl.col("stream_sym") == f"{stream_prefix}@aggtrade").sort("T_ms")

        if len(sym_fills) == 0 or len(sym_agg) == 0:
            log.warning("no_data_for_symbol_clock_test", symbol=sym)
            continue

        joined = sym_fills.join_asof(
            sym_agg.select(["T_ms"]),
            left_on="t_ws_ms",
            right_on="T_ms",
            strategy="nearest",
        )

        delta_ms: npt.NDArray[np.float64] = (
            (joined["t_ws_ms"] - joined["T_ms"]).to_numpy().astype(np.float64)
        )

        median_ms = float(np.median(np.abs(delta_ms)))
        p99_ms = float(np.percentile(np.abs(delta_ms), 99))

        ax.hist(delta_ms, bins=50)
        ax.axvline(median_ms, color="r", linestyle="--", label=f"median={median_ms:.0f}ms")
        ax.set_title(f"{sym}")
        ax.set_xlabel("t_ws_ms - T_ms (ms)")
        ax.legend()

        results[sym] = {
            "median_ms": round(median_ms, 1),
            "p99_ms": round(p99_ms, 1),
            "sample_n": len(joined),
            "gate_pass": bool(median_ms < 100.0),
        }
        log.info("clock_test2_symbol", symbol=sym, **results[sym])

    fig.tight_layout()
    out_path = plots_dir / "clock_binance_vs_pmclob.png"
    fig.savefig(str(out_path), dpi=150)
    plt.close(fig)
    return results
