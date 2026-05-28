"""Phase 2: First-order stats and inventory plots for the Phase 2 stats report.

Computes and saves:
1. Maker:taker ratio (headline: 100% maker confirmed)
2. Side balance (SELL vs BUY by count and notional)
3. Fills per market (by token_id)
4. Price distribution at fill (proxy for ITM vs OTM, horizon mix)
5. Inventory analysis (A-S inventory aversion check):
   a. Net position over 5m market lifecycle (10 sampled markets)
   b. Total dollar exposure over the 24h window
   c. Distribution of peak inventory per market

All plots saved to output/plots/. Stats written to output/results/phase2_stats.json.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import polars as pl
import structlog

from reverse_engineering.config import get_settings
from reverse_engineering.tables.inventory import (
    build_inventory_series,
    compute_peak_inventory_per_market,
    compute_total_dollar_exposure_series,
)

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)


def compute_first_order_stats(fills: pl.DataFrame) -> dict[str, Any]:
    """Compute Phase 2 first-order statistics.

    Args:
        fills: ohanism_fills DataFrame.

    Returns:
        Dict of statistics for RESULTS.md.
    """
    total = len(fills)
    maker_count = fills.filter(pl.col("is_maker")).height
    taker_count = total - maker_count

    sell_count = fills.filter(pl.col("ohanism_side") == "SELL").height
    buy_count = fills.filter(pl.col("ohanism_side") == "BUY").height

    sell_notional = fills.filter(pl.col("ohanism_side") == "SELL")["size"].cast(pl.Float64).sum()
    buy_notional = fills.filter(pl.col("ohanism_side") == "BUY")["size"].cast(pl.Float64).sum()

    fills_per_token = fills.group_by("token_id").len().sort("len", descending=True)

    builder_counts = fills["builder"].value_counts().sort("count", descending=True)
    top_builder = builder_counts["builder"][0] if len(builder_counts) else "N/A"

    price_arr = fills["price"].cast(pl.Float64).to_numpy()
    price_percentiles = {
        "p5": float(np.percentile(price_arr, 5)),
        "p25": float(np.percentile(price_arr, 25)),
        "p50": float(np.percentile(price_arr, 50)),
        "p75": float(np.percentile(price_arr, 75)),
        "p95": float(np.percentile(price_arr, 95)),
    }

    stats: dict[str, Any] = {
        "total_fills": total,
        "maker_count": maker_count,
        "taker_count": taker_count,
        "maker_pct": round(maker_count / total * 100, 2),
        "sell_count": sell_count,
        "buy_count": buy_count,
        "sell_pct": round(sell_count / total * 100, 2),
        "buy_pct": round(buy_count / total * 100, 2),
        "sell_notional": round(float(sell_notional or 0), 2),
        "buy_notional": round(float(buy_notional or 0), 2),
        "unique_tokens": fills["token_id"].n_unique(),
        "fills_per_token_median": fills_per_token["len"].cast(pl.Float64).median() or 0.0,
        "fills_per_token_max": fills_per_token["len"].cast(pl.Int64).max() or 0,
        "fills_per_token_p90": fills_per_token["len"].cast(pl.Float64).quantile(0.9) or 0.0,
        "top_builder": str(top_builder),
        "direct_submission_pct": round(
            fills.filter(pl.col("builder") == "0" * 64).height / total * 100, 2
        ),
        "price_percentiles": price_percentiles,
    }

    if "horizon" in fills.columns:
        horizon_counts = fills.group_by("horizon").len().sort("len", descending=True)
        stats["horizon_distribution"] = dict(
            zip(
                horizon_counts["horizon"].to_list(),
                horizon_counts["len"].to_list(),
                strict=False,
            )
        )
    if "asset_symbol" in fills.columns:
        asset_counts = fills.group_by("asset_symbol").len().sort("len", descending=True)
        stats["asset_distribution"] = dict(
            zip(
                asset_counts["asset_symbol"].to_list(),
                asset_counts["len"].to_list(),
                strict=False,
            )
        )

    return stats


def plot_inventory_lifecycle(
    inv: pl.DataFrame,
    n_markets: int = 10,
    plots_dir: Path | None = None,
) -> None:
    """Plot net position over the 5m market lifecycle for sampled markets.

    Args:
        inv: Output of build_inventory_series().
        n_markets: Number of markets to sample and plot.
        plots_dir: Output directory. Defaults to Settings.plots_dir.
    """
    if plots_dir is None:
        plots_dir = get_settings().plots_dir
    plots_dir.mkdir(parents=True, exist_ok=True)

    tokens = inv["token_id"].unique().to_list()
    if len(tokens) > n_markets:
        rng = np.random.default_rng(42)
        tokens = rng.choice(tokens, size=n_markets, replace=False).tolist()

    fig, axes = plt.subplots(2, 5, figsize=(20, 8), sharey=False)
    axes_flat = axes.flatten()

    for idx, tid in enumerate(tokens[:n_markets]):
        ax = axes_flat[idx]
        market_inv = inv.filter(pl.col("token_id") == tid).sort("block_number")
        t_ns = market_inv["t_block_ns"].to_numpy().astype(float)
        pos = market_inv["cum_position"].to_numpy()
        t_min = t_ns.min()
        t_rel = (t_ns - t_min) / 1e9

        ax.step(t_rel, pos, where="post", linewidth=1.2)
        ax.axhline(0, color="gray", linewidth=0.5, linestyle="--")
        ax.fill_between(t_rel, pos, 0, alpha=0.15, step="post")
        ax.set_xlabel("Seconds into market", fontsize=8)
        ax.set_ylabel("Net position (tokens)", fontsize=8)
        ax.set_title(f"Token ...{tid[-6:]}", fontsize=8)
        ax.tick_params(labelsize=7)

    for idx in range(len(tokens), n_markets):
        axes_flat[idx].set_visible(False)

    fig.suptitle("Net position over 5m market lifecycle (10 sampled markets)", fontsize=12)
    fig.tight_layout()
    out = plots_dir / "inventory_lifecycle.png"
    fig.savefig(str(out), dpi=150)
    plt.close(fig)
    log.info("inventory_lifecycle_plot_saved", path=str(out))


def plot_total_exposure(
    exposure_series: pl.DataFrame,
    plots_dir: Path | None = None,
) -> dict[str, float]:
    """Plot total dollar exposure over the analysis window.

    Args:
        exposure_series: Output of compute_total_dollar_exposure_series().
        plots_dir: Output directory.

    Returns:
        Dict with exposure stats: max, mean, p90, p95.
    """
    if plots_dir is None:
        plots_dir = get_settings().plots_dir
    plots_dir.mkdir(parents=True, exist_ok=True)

    t_ns = exposure_series["t_block_ns"].to_numpy().astype(float)
    exp = exposure_series["total_dollar_exposure"].to_numpy()
    t_hr = (t_ns - t_ns.min()) / 3.6e12

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(t_hr, exp, alpha=0.4)
    ax.plot(t_hr, exp, linewidth=0.7)
    ax.set_xlabel("Hours into analysis window")
    ax.set_ylabel("Total dollar exposure (USDC)")
    ax.set_title("ohanism total dollar exposure across all open positions")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    fig.tight_layout()
    out = plots_dir / "total_dollar_exposure.png"
    fig.savefig(str(out), dpi=150)
    plt.close(fig)
    log.info("total_exposure_plot_saved", path=str(out))

    return {
        "max_exposure": float(exp.max()),
        "mean_exposure": float(exp.mean()),
        "p90_exposure": float(np.percentile(exp, 90)),
        "p95_exposure": float(np.percentile(exp, 95)),
    }


def plot_peak_inventory_distribution(
    peaks: pl.DataFrame,
    plots_dir: Path | None = None,
) -> dict[str, float]:
    """Plot distribution of peak absolute inventory per market.

    Args:
        peaks: Output of compute_peak_inventory_per_market().
        plots_dir: Output directory.

    Returns:
        Dict with peak inventory stats.
    """
    if plots_dir is None:
        plots_dir = get_settings().plots_dir
    plots_dir.mkdir(parents=True, exist_ok=True)

    peak_abs = peaks["peak_abs"].to_numpy()
    final_pos = peaks["final_position"].to_numpy()

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    axes[0].hist(peak_abs, bins=50, edgecolor="k", linewidth=0.3)
    axes[0].set_xlabel("Peak absolute position (tokens)")
    axes[0].set_ylabel("Market count")
    axes[0].set_title("Distribution of peak inventory per market")
    axes[0].set_yscale("log")

    axes[1].hist(final_pos, bins=50, edgecolor="k", linewidth=0.3)
    axes[1].axvline(0, color="r", linestyle="--", linewidth=1, label="Zero")
    axes[1].set_xlabel("Final position at market expiry (tokens)")
    axes[1].set_ylabel("Market count")
    axes[1].set_title("Final inventory at market close")
    axes[1].legend()

    fig.tight_layout()
    out = plots_dir / "peak_inventory_distribution.png"
    fig.savefig(str(out), dpi=150)
    plt.close(fig)
    log.info("peak_inventory_plot_saved", path=str(out))

    return {
        "median_peak_abs": float(np.median(peak_abs)),
        "p90_peak_abs": float(np.percentile(peak_abs, 90)),
        "p95_peak_abs": float(np.percentile(peak_abs, 95)),
        "pct_net_zero": float((np.abs(final_pos) < 0.001).mean() * 100),
        "median_final_abs": float(np.median(np.abs(final_pos))),
    }


def run_phase2_analysis(fills: pl.DataFrame) -> dict[str, Any]:
    """Run all Phase 2 analyses and write plots + results JSON.

    Args:
        fills: ohanism_fills DataFrame.

    Returns:
        Combined results dict.
    """
    cfg = get_settings()
    cfg.plots_dir.mkdir(parents=True, exist_ok=True)
    cfg.results_dir.mkdir(parents=True, exist_ok=True)

    log.info("phase2_analysis_start", fills=len(fills))

    # First-order stats
    stats = compute_first_order_stats(fills)
    log.info(
        "first_order_stats", **{k: str(v) for k, v in stats.items() if not isinstance(v, dict)}
    )

    # Inventory analysis
    inv = build_inventory_series(fills)
    inv.write_parquet(str(cfg.tables_dir / "inventory_series.parquet"))

    peaks = compute_peak_inventory_per_market(inv)
    peaks.write_parquet(str(cfg.tables_dir / "inventory_peaks.parquet"))

    exposure_series = compute_total_dollar_exposure_series(inv)
    exposure_series.write_parquet(str(cfg.tables_dir / "dollar_exposure_series.parquet"))

    # Plots
    plot_inventory_lifecycle(inv, n_markets=10, plots_dir=cfg.plots_dir)
    exposure_stats = plot_total_exposure(exposure_series, plots_dir=cfg.plots_dir)
    peak_stats = plot_peak_inventory_distribution(peaks, plots_dir=cfg.plots_dir)

    results: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "phase": 2,
        "first_order_stats": stats,
        "exposure_stats": exposure_stats,
        "peak_inventory_stats": peak_stats,
    }

    out_path = cfg.results_dir / "phase2_stats.json"
    out_path.write_text(json.dumps(results, indent=2))
    log.info("phase2_stats_written", path=str(out_path))
    return results
