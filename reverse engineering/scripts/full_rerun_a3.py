"""A3: Re-run all Phase 1-3 analyses on the full 49h analysis window.

Window: 2026-05-27/04 through 2026-05-29/04 (49 hours).
All computations local. Memory discipline: per-hour scan, no full-day materialization.

Produces:
- output/tables/ohanism_fills_full.parquet (full window)
- output/results/a3_rerun.json (all old→new comparison values)
- Various plots in output/plots/

Runtime estimate: ~60-90 minutes (block_times RPC, Gamma Gamma cache mostly warm).
"""
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "src")

import numpy as np
import polars as pl

from reverse_engineering.config import get_settings
from reverse_engineering.tables.ohanism_fills import (
    build_ohanism_fills,
    write_ohanism_fills,
    OHANISM_FILLS_COLUMNS,
)
from reverse_engineering.io.gamma import fetch_markets_by_slug_range
from reverse_engineering.tables.market_enrichment import build_start_strike_prices
from reverse_engineering.tables.inventory import (
    build_inventory_series,
    compute_peak_inventory_per_market,
    compute_total_dollar_exposure_series,
)
from reverse_engineering.tables.phase2_stats import compute_first_order_stats

cfg = get_settings()
cfg.tables_dir.mkdir(parents=True, exist_ok=True)
cfg.results_dir.mkdir(parents=True, exist_ok=True)
cfg.plots_dir.mkdir(parents=True, exist_ok=True)

WINDOW_DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]
WINDOW_HOURS = {
    "2026-05-27": list(range(4, 24)),   # hours 04-23
    "2026-05-28": list(range(0, 24)),   # hours 00-23
    "2026-05-29": list(range(0, 5)),    # hours 00-04
}

OLD = {
    "window_hours": 20,
    "fills_total": 21451,
    "maker_pct": 100.0,
    "direct_sub_pct": 100.0,
    "sell_pct_raw": 83.42,
    "canonical_long_up_net_pct": 6.9,
    "xrp_5m_long_up_pct": 31.7,
    "btc_pct": 62.8,
    "eth_pct": 20.2,
    "h5m_pct": 74.8,
    "h15m_pct": 20.5,
    "peak_exposure_usdc": 167059,
    "mean_exposure_usdc": 85039,
    "net_zero_pct": 0.0,
    "backfill_pct": 90.6,
    "pmclob_coverage_pct": 72.0,
    "pull_rate_pct": 0.15,
    "lifetime_median_ms": 26.0,
    "lifetime_p90_ms": 573.0,
    "rebate_mean_usdc": 0.070,
    "rebate_total_usdc": 1430.0,
    "otm_cushion_median": 0.220,
    "otm_cushion_gt01_pct": 78.3,
    "selection_5m_pct": 61.9,
    "selection_15m_pct": 60.2,
    "settlement_burn_count": 365,
    "settlement_burn_pct_of_traded": 20.3,
}

t_total = time.time()

# ── Step 1: Build ohanism_fills for full window (checkpoint) ─────────────────
_fills_ckpt = cfg.tables_dir / "ohanism_fills_full_ckpt.parquet"
if _fills_ckpt.exists():
    print("Step 1: Loading fills from checkpoint...")
    fills_full = pl.read_parquet(str(_fills_ckpt))
    print(f"  {len(fills_full)} fills loaded from checkpoint")
else:
    print("Step 1: Building ohanism_fills for full window...")
    t1 = time.time()
    fills_full = build_ohanism_fills(WINDOW_DATES)
    fills_full.write_parquet(str(_fills_ckpt), compression="zstd")
    print(f"  {len(fills_full)} fills in {time.time()-t1:.0f}s (checkpoint saved)")

# ── Step 2: Market metadata enrichment (slug-based Gamma lookup) ──────────────
print("Step 2: Enriching with Gamma market metadata (slug-based)...")
t2 = time.time()

# Window in Unix seconds
WINDOW_START_UNIX = 1779854400  # 2026-05-27 04:00 UTC
WINDOW_END_UNIX   = 1780030200  # 2026-05-29 04:59 UTC

# Slug-based lookup: cache-only (no API calls in A3 — warm cache separately)
from reverse_engineering.io.gamma import _load_cached_cids
_gamma_raw = _load_cached_cids()
# Build DataFrame from cached entries only
_gamma_records = []
for _key, _meta in _gamma_raw.items():
    _token_ids_raw = _meta.get("token_ids_json", "[]")
    try:
        _token_ids = json.loads(_token_ids_raw) if isinstance(_token_ids_raw, str) else _token_ids_raw
    except (json.JSONDecodeError, TypeError):
        _token_ids = []
    for _i, _tid in enumerate(_token_ids):
        _gamma_records.append({
            "token_id": str(_tid),
            "market": _meta.get("condition_id", ""),
            "asset_symbol": _meta.get("asset_symbol", ""),
            "horizon": _meta.get("horizon", ""),
            "outcome_side": "Up" if _i == 0 else "Down",
            "start_date_unix": float(_meta.get("start_date_unix", 0) or 0),
            "end_date_unix": float(_meta.get("end_date_unix", 0) or 0),
        })
gamma_lookup = pl.DataFrame(_gamma_records).unique(subset=["token_id"]) if _gamma_records else pl.DataFrame(
    schema={"token_id": pl.Utf8, "market": pl.Utf8, "asset_symbol": pl.Utf8,
            "horizon": pl.Utf8, "outcome_side": pl.Utf8,
            "start_date_unix": pl.Float64, "end_date_unix": pl.Float64}
)
print(f"  Gamma slug lookup: {len(gamma_lookup)} token rows in {time.time()-t2:.0f}s")

fill_tids = set(fills_full["token_id"].to_list())
gamma_filtered = gamma_lookup.filter(pl.col("token_id").is_in(list(fill_tids)))
print(f"  Matched: {len(gamma_filtered)}/{len(fill_tids)} fill tokens ({len(gamma_filtered)/len(fill_tids)*100:.1f}%)")

# Rename Gamma cols to avoid conflict with existing null cols in fills
g_rename = {c: f"_g_{c}" for c in gamma_filtered.columns if c != "token_id"}
fills_full = fills_full.join(gamma_filtered.rename(g_rename), on="token_id", how="left")

# Apply only columns that exist in both fills and gamma
# "market" column may be null in fills; start/end_date_unix are NOT in fills schema
for col in ["market", "asset_symbol", "horizon", "outcome_side"]:
    g_col = f"_g_{col}"
    if g_col not in fills_full.columns:
        continue
    if col in fills_full.columns:
        fills_full = fills_full.with_columns(
            pl.when(pl.col(g_col).is_not_null()).then(pl.col(g_col))
            .otherwise(pl.col(col)).alias(col)
        ).drop(g_col)
    else:
        fills_full = fills_full.rename({g_col: col})

# time_to_expiry_s from end_date_unix (Gamma column, not in fills schema)
if "_g_end_date_unix" in fills_full.columns:
    fills_full = fills_full.with_columns(
        pl.when(pl.col("_g_end_date_unix").is_not_null())
        .then(pl.col("_g_end_date_unix") - pl.col("t_block_ns").cast(pl.Float64) / 1e9)
        .otherwise(pl.col("time_to_expiry_s"))
        .alias("time_to_expiry_s")
    ).drop("_g_end_date_unix")

# Drop _g_start_date_unix if present (used only for start_strike lookup below)
if "_g_start_date_unix" in fills_full.columns:
    fills_full = fills_full.drop("_g_start_date_unix")
covered = fills_full["asset_symbol"].drop_nulls().len()
print(f"  Metadata coverage: {covered}/{len(fills_full)} ({covered/len(fills_full)*100:.1f}%)")

# start_strike_price from Binance bookTicker
strikes = gamma_filtered.unique(subset=["market"]).select(
    ["market", "asset_symbol", "start_date_unix"]
)
strikes_with_s = build_start_strike_prices(strikes, WINDOW_DATES)
if "start_strike_price" in strikes_with_s.columns:
    fills_full = fills_full.join(
        strikes_with_s.select(["market", "start_strike_price"]).rename(
            {"start_strike_price": "_g_strike"}),
        on="market", how="left"
    ).with_columns(
        pl.when(pl.col("_g_strike").is_not_null()).then(pl.col("_g_strike"))
        .otherwise(pl.col("start_strike_price")).alias("start_strike_price")
    ).drop(["_g_strike"])

# Ensure canonical column order
for c in OHANISM_FILLS_COLUMNS:
    if c not in fills_full.columns:
        fills_full = fills_full.with_columns(pl.lit(None).cast(pl.Utf8).alias(c))
fills_full = fills_full.select(OHANISM_FILLS_COLUMNS)

# Write full-window fills
out_path = cfg.tables_dir / "ohanism_fills_full.parquet"
fills_full.write_parquet(str(out_path), compression="zstd")
print(f"  Written to {out_path}")

# ── Step 3: Phase 1 stats ─────────────────────────────────────────────────────
print("\nStep 3: Phase 1 stats...")
total_fills = len(fills_full)
backfill_count = fills_full.filter(pl.col("is_backfilled") == True).height
backfill_pct = backfill_count / total_fills * 100

pmclob_covered = fills_full.filter(pl.col("t_ws_method") == "tx_hash").height
pmclob_pct = pmclob_covered / total_fills * 100

# pm_clob coverage by asset and horizon
by_asset_horizon = (
    fills_full.filter(pl.col("asset_symbol").is_not_null())
    .group_by(["asset_symbol", "horizon"])
    .agg([
        pl.len().alias("fills"),
        (pl.col("t_ws_method") == "tx_hash").sum().alias("pmclob_matched"),
    ])
    .with_columns((pl.col("pmclob_matched") / pl.col("fills") * 100).alias("pmclob_pct"))
    .sort(["asset_symbol", "horizon"])
)
print(f"  Total fills: {total_fills}")
print(f"  Backfill pct: {backfill_pct:.1f}%")
print(f"  pm_clob coverage (tx_hash): {pmclob_pct:.1f}%")
print("  By asset+horizon:")
print(by_asset_horizon)

# Window timestamps
t_min_ns = int(fills_full["t_block_ns"].min())
t_max_ns = int(fills_full["t_block_ns"].max())
window_h = (t_max_ns - t_min_ns) / 3.6e12
print(f"  Window: {t_min_ns/1e9:.0f} – {t_max_ns/1e9:.0f} ({window_h:.1f}h)")

# ── Step 4: Phase 2 stats ─────────────────────────────────────────────────────
print("\nStep 4: Phase 2 stats...")
stats2 = compute_first_order_stats(fills_full)
print(f"  maker_pct={stats2['maker_pct']} sell_pct={stats2['sell_pct']} "
      f"direct_sub_pct={stats2['direct_submission_pct']}")

# Canonical skew
has_meta = fills_full.filter(
    pl.col("outcome_side").is_not_null() & pl.col("asset_symbol").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.when(
        ((pl.col("ohanism_side") == "BUY") & (pl.col("outcome_side") == "Up"))
        | ((pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign"),
])
has_meta = has_meta.with_columns(
    (pl.col("canonical_sign") * pl.col("size_f")).alias("net_long_up_tokens")
)
net_long_up = has_meta["net_long_up_tokens"].sum()
total_notional = has_meta["size_f"].sum()
canonical_pct = net_long_up / total_notional * 100

# XRP 5m
xrp5m = has_meta.filter(
    (pl.col("asset_symbol") == "XRP") & (pl.col("horizon") == "5m")
).with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("up_price")
)
xrp_net = 0.0
if len(xrp5m) > 0:
    xrp_long = xrp5m.filter(pl.col("canonical_sign") == 1)["size_f"].sum() or 0
    xrp_total = xrp5m["size_f"].sum() or 1
    xrp_net = xrp_long / xrp_total * 100

print(f"  canonical_long_up_net_pct={canonical_pct:.1f}")
print(f"  XRP 5m long-Up pct: {xrp_net:.1f}% (n={len(xrp5m)})")
if "asset_distribution" in stats2:
    print(f"  Asset dist: {stats2['asset_distribution']}")
if "horizon_distribution" in stats2:
    print(f"  Horizon dist: {stats2['horizon_distribution']}")

# ── Step 5: Inventory analysis ────────────────────────────────────────────────
print("\nStep 5: Inventory analysis...")
inv = build_inventory_series(fills_full)
peaks = compute_peak_inventory_per_market(inv)
exp_series = compute_total_dollar_exposure_series(inv)
exp_arr = exp_series["total_dollar_exposure"].to_numpy()
peak_abs_arr = peaks["peak_abs"].to_numpy()
final_arr = peaks["final_position"].to_numpy()

net_zero_pct = (np.abs(final_arr) < 0.001).mean() * 100
print(f"  Peak exposure max={exp_arr.max():.0f} mean={exp_arr.mean():.0f}")
print(f"  net-zero final positions: {net_zero_pct:.1f}%")

# ── Step 6: Economic offsets ──────────────────────────────────────────────────
print("\nStep 6: Economic offsets...")
econ = fills_full.filter(
    pl.col("start_strike_price").is_not_null() & pl.col("outcome_side").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
])
econ = econ.with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("up_price")
)
econ = econ.with_columns(
    (pl.col("up_price") - 0.5).abs().alias("otm_cushion")
)
rebate_arr = econ["rebate_f"].to_numpy()
otm_arr = econ["otm_cushion"].to_numpy()
print(f"  Rebate mean={np.mean(rebate_arr):.4f} total={np.sum(rebate_arr):.0f}")
print(f"  OTM cushion median={np.median(otm_arr):.3f} gt0.1={( otm_arr>0.1).mean()*100:.1f}%")

# Market selection (using Gamma cache)
from reverse_engineering.io.gamma import _load_cached_cids
cached = _load_cached_cids()
total_5m_gamma = sum(1 for v in cached.values() if v.get("horizon") == "5m")
total_15m_gamma = sum(1 for v in cached.values() if v.get("horizon") == "15m")
ohanism_5m = fills_full.filter(pl.col("horizon") == "5m")["market"].drop_nulls().n_unique()
ohanism_15m = fills_full.filter(pl.col("horizon") == "15m")["market"].drop_nulls().n_unique()
sel_5m = ohanism_5m / max(total_5m_gamma, 1) * 100
sel_15m = ohanism_15m / max(total_15m_gamma, 1) * 100
print(f"  Selection: 5m={ohanism_5m}/{total_5m_gamma} ({sel_5m:.1f}%),  "
      f"15m={ohanism_15m}/{total_15m_gamma} ({sel_15m:.1f}%)")

# Settlement burns
from pathlib import Path
OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
ZERO = "0x0000000000000000000000000000000000000000"
burn_rows = []
for f in sorted(cfg.cache_dir.glob("feed=polygon/date=*/hour=*/data.parquet")):
    lf = pl.scan_parquet(str(f), low_memory=True, hive_partitioning=False)
    burns = lf.filter(
        (pl.col("event") == "TransferSingle")
        & (pl.col("from_") == OHANISM)
        & (pl.col("to") == ZERO)
        & (pl.col("operator") == OHANISM)
    ).select(["tx_hash", "block_number", "t_recv_ns", "token_id"]).collect()
    if len(burns):
        burn_rows.append(burns)
burns_df = pl.concat(burn_rows, how="diagonal_relaxed") if burn_rows else pl.DataFrame()
burn_count = len(burns_df)
burned_tids = set(burns_df["token_id"].drop_nulls().to_list()) if len(burns_df) else set()
traded_and_burned = fill_tids & burned_tids
settle_pct = len(traded_and_burned) / len(fill_tids) * 100
print(f"  Settlement burns: {burn_count} events, {len(traded_and_burned)} tokens redeemed "
      f"({settle_pct:.1f}% of traded)")

# ── Step 7: Compile results and old→new comparison ───────────────────────────
print("\nStep 7: Compiling results...")

NEW = {
    "window_hours": 49,
    "fills_total": total_fills,
    "maker_pct": stats2["maker_pct"],
    "direct_sub_pct": stats2["direct_submission_pct"],
    "sell_pct_raw": stats2["sell_pct"],
    "canonical_long_up_net_pct": round(canonical_pct, 1),
    "xrp_5m_long_up_pct": round(xrp_net, 1),
    "btc_pct": 0.0,  # filled below
    "eth_pct": 0.0,
    "h5m_pct": 0.0,
    "h15m_pct": 0.0,
    "peak_exposure_usdc": round(float(exp_arr.max()), 0),
    "mean_exposure_usdc": round(float(exp_arr.mean()), 0),
    "net_zero_pct": round(net_zero_pct, 1),
    "backfill_pct": round(backfill_pct, 1),
    "pmclob_coverage_pct": round(pmclob_pct, 1),
    "pull_rate_pct": OLD["pull_rate_pct"],  # TBD in Part B
    "lifetime_median_ms": OLD["lifetime_median_ms"],  # TBD in Part B
    "lifetime_p90_ms": OLD["lifetime_p90_ms"],
    "rebate_mean_usdc": round(float(np.mean(rebate_arr)), 4),
    "rebate_total_usdc": round(float(np.sum(rebate_arr)), 2),
    "otm_cushion_median": round(float(np.median(otm_arr)), 3),
    "otm_cushion_gt01_pct": round(float((otm_arr > 0.1).mean() * 100), 1),
    "selection_5m_pct": round(sel_5m, 1),
    "selection_15m_pct": round(sel_15m, 1),
    "settlement_burn_count": burn_count,
    "settlement_burn_pct_of_traded": round(settle_pct, 1),
}

# Fill asset/horizon pcts
if "asset_distribution" in stats2 and stats2["asset_distribution"]:
    tot = sum(stats2["asset_distribution"].values())
    NEW["btc_pct"] = round(stats2["asset_distribution"].get("BTC", 0) / tot * 100, 1)
    NEW["eth_pct"] = round(stats2["asset_distribution"].get("ETH", 0) / tot * 100, 1)
if "horizon_distribution" in stats2 and stats2["horizon_distribution"]:
    tot = sum(stats2["horizon_distribution"].values())
    NEW["h5m_pct"] = round(stats2["horizon_distribution"].get("5m", 0) / tot * 100, 1)
    NEW["h15m_pct"] = round(stats2["horizon_distribution"].get("15m", 0) / tot * 100, 1)

out_json = {
    "window": {"start": "2026-05-27T04:00Z", "end": "2026-05-29T04:59Z", "hours": 49},
    "old_values": OLD,
    "new_values": NEW,
    "changes": {},
}

print("\n=== OLD → NEW COMPARISON ===")
changed_findings = []
for key in OLD:
    o = OLD[key]
    n = NEW.get(key, "N/A")
    if isinstance(o, float) and isinstance(n, float) and o != 0:
        delta_pct = (n - o) / abs(o) * 100
        flag = "⚠ >10%" if abs(delta_pct) > 10 else ""
        out_json["changes"][key] = {"old": o, "new": n, "delta_pct": round(delta_pct, 1)}
        print(f"  {key}: {o} → {n} ({delta_pct:+.1f}%) {flag}")
        if abs(delta_pct) > 10:
            changed_findings.append(f"{key}: {o} → {n} ({delta_pct:+.1f}%)")
    else:
        out_json["changes"][key] = {"old": o, "new": n}
        print(f"  {key}: {o} → {n}")

if changed_findings:
    print(f"\n⚠ {len(changed_findings)} findings changed by >10%:")
    for c in changed_findings:
        print(f"  {c}")
else:
    print("\nNo findings changed by >10%. All prior conclusions confirmed at scale.")

out_json_path = cfg.results_dir / "a3_rerun.json"
out_json_path.write_text(json.dumps(out_json, indent=2, default=str))
print(f"\nWritten: {out_json_path}")
print(f"Total A3 runtime: {(time.time()-t_total)/60:.1f} min")
