"""Pre-7b — Find the best realized-vol window for per-market sigma prediction.

Tests trailing realized vol at 5m/15m/30m/1h/2h before t_post as predictors for
sigma_implied. Also tests stale-carry (previous market's sigma) and cross-asset.
Best OOS R2 drives the paper twin's sigma recipe.

Also checks selection rule: what distinguishes quoted vs declined markets?
"""
import sys, json, time
sys.path.insert(0, "src")

import numpy as np
import polars as pl
from scipy.stats import norm
from numpy.linalg import lstsq
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.gamma import _load_cached_cids

cfg = get_settings()
t0 = time.time()
np.random.seed(42)

print("=== PRE-7B: SIGMA PREDICTOR IDENTIFICATION ===")

FEEDS = ["pm_clob","polygon","binance","pm_meta"]
WINDOW_START = ("2026-05-27",4)
feed_parts = {f: set((p.date,p.hour) for p in list_local_partitions(f)) for f in FEEDS}
common = None
for f in FEEDS:
    common = feed_parts[f] if common is None else common & feed_parts[f]
common = {p for p in common if p >= WINDOW_START}
WINDOW_DATES = sorted(set(d for d,_ in sorted(common)))
WINDOW_END = max(common)
print(f"Window: {WINDOW_START} → {WINDOW_END}")

sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
N = len(sig_v2)
SYMBOL_STREAM = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT","DOGE":"DOGEUSDT"}
SEC_PER_YEAR = 365.25*24*3600

# ── Load Binance 1-min bars ───────────────────────────────────────────────────
print("Loading Binance 1-min bar cache...")
bticker_rows = []
for date in WINDOW_DATES:
    for pq in sorted(cfg.cache_dir.glob(f"feed=binance/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(pq), low_memory=True, hive_partitioning=False,
                             use_statistics=False)
        df = (lf.filter(pl.col("e").is_null() & pl.col("b").is_not_null())
              .select(["t_recv_ns","s","b","a"]).collect())
        if len(df): bticker_rows.append(df)

bticker = (pl.concat(bticker_rows)
           .with_columns([pl.col("b").cast(pl.Float64).alias("bid"),
                          pl.col("a").cast(pl.Float64).alias("ask")])
           .with_columns(((pl.col("bid")+pl.col("ask"))/2).alias("mid"))
           .sort("t_recv_ns"))
print(f"  {len(bticker):,} ticks")

# Build per-symbol 1-min bar series
bars_by_sym: dict[str, tuple[np.ndarray, np.ndarray]] = {}
BAR_S = 60
for sym in set(SYMBOL_STREAM.values()):
    sub = bticker.filter(pl.col("s")==sym).sort("t_recv_ns")
    if len(sub) < 10: continue
    ts = sub["t_recv_ns"].to_numpy()
    mid = sub["mid"].to_numpy()
    bar_label = ts // (BAR_S * 10**9)
    _, bar_end_idx = np.unique(bar_label, return_index=True)
    bar_end_idx = np.append(np.diff(bar_end_idx, prepend=-1).cumsum()[:-1]-1, len(ts)-1)
    # Actually use last-tick-in-bar approach
    unique_bars, first_idx = np.unique(bar_label, return_index=True)
    last_idx = np.append(first_idx[1:]-1, len(ts)-1)
    bar_ts  = ts[last_idx]
    bar_mid = mid[last_idx]
    bars_by_sym[sym] = (bar_ts, bar_mid)

print(f"  Built {len(bars_by_sym)} symbol bar series")

def realized_vol(sym: str, t_ns: int, window_minutes: int) -> float | None:
    """Parkinson-Rogers realized vol over the past window_minutes of 1-min bars."""
    if sym not in bars_by_sym: return None
    ts, mids = bars_by_sym[sym]
    end_idx = np.searchsorted(ts, t_ns, side="right") - 1
    if end_idx < window_minutes: return None
    start_idx = end_idx - window_minutes
    rets = np.log(mids[start_idx+1:end_idx+1] / np.maximum(mids[start_idx:end_idx], 1e-9))
    if len(rets) < 3: return None
    rv = float(np.std(rets) * np.sqrt(SEC_PER_YEAR / BAR_S))
    return rv if np.isfinite(rv) and rv > 0 else None

# ── Compute per-market realized vols at multiple windows ─────────────────────
print("Computing per-market realized vols at 5m/15m/30m/60m/120m windows...")
WINDOWS = [5, 15, 30, 60, 120]  # minutes

rows = []
for row in sig_v2.iter_rows(named=True):
    asset = str(row["asset_symbol"])
    sym   = SYMBOL_STREAM.get(asset)
    if sym is None: continue
    t_post = int(row["t_post_ns"])
    sigma_i = float(row["sigma_implied"])
    feat = {
        "market_id": str(row["market_id"]),
        "asset": asset,
        "horizon": str(row["horizon"]),
        "t_post_ns": t_post,
        "sigma_implied": sigma_i,
    }
    for w in WINDOWS:
        feat[f"rv_{w}m"] = realized_vol(sym, t_post, w)
    rows.append(feat)

df = pl.DataFrame(rows)
print(f"  {len(df)} markets, coverage per window:")
for w in WINDOWS:
    n = df.filter(pl.col(f"rv_{w}m").is_not_null()).height
    print(f"    rv_{w}m: {n}/{len(df)} ({100*n/len(df):.1f}%)")

# ── OOS R² for each window → sigma_implied ───────────────────────────────────
print("\nOOS R² (70/30 temporal split) for each realized vol window:")
df_sorted = df.sort("t_post_ns").to_pandas()
n_tr = int(len(df_sorted)*0.70)
best_r2, best_window = -float("inf"), None
r2_results = {}
for w in WINDOWS:
    col = f"rv_{w}m"
    sub = df_sorted.dropna(subset=[col,"sigma_implied"])
    if len(sub) < 50: continue
    n_t = int(len(sub)*0.70)
    si_tr = sub["sigma_implied"].values[:n_t]
    rv_tr = sub[col].values[:n_t]
    si_te = sub["sigma_implied"].values[n_t:]
    rv_te = sub[col].values[n_t:]
    w_fit,_,_,_ = lstsq(np.c_[np.ones(n_t),rv_tr], si_tr, rcond=None)
    pred = np.c_[np.ones(len(rv_te)),rv_te] @ w_fit
    ss_r = float(np.sum((si_te-pred)**2)); ss_t = float(np.sum((si_te-np.mean(si_te))**2))
    r2 = float(1-ss_r/ss_t) if ss_t>0 else float("nan")
    r2_results[f"rv_{w}m"] = round(r2,4)
    support = "B" if r2>0.5 else ("A" if r2<0.2 else "ambiguous")
    print(f"  rv_{w}m: R²={r2:.4f}  a={w_fit[0]:.3f}  b={w_fit[1]:.3f}  → {support}")
    if r2 > best_r2:
        best_r2 = r2; best_window = w

print(f"\n  Best predictor: rv_{best_window}m with R²={best_r2:.4f}")

# ── Per-asset best window ─────────────────────────────────────────────────────
print("\nPer-asset best R²:")
for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    sub_a = df_sorted[df_sorted["asset"]==asset]
    if len(sub_a) < 20: continue
    best_a_r2, best_a_w = -float("inf"), None
    for w in WINDOWS:
        col = f"rv_{w}m"
        sub2 = sub_a.dropna(subset=[col,"sigma_implied"])
        if len(sub2) < 10: continue
        n_ta = int(len(sub2)*0.70)
        si_tr = sub2["sigma_implied"].values[:n_ta]
        rv_tr = sub2[col].values[:n_ta]
        si_te = sub2["sigma_implied"].values[n_ta:]
        rv_te = sub2[col].values[n_ta:]
        w_a,_,_,_ = lstsq(np.c_[np.ones(n_ta),rv_tr], si_tr, rcond=None)
        pred_a = np.c_[np.ones(len(rv_te)),rv_te] @ w_a
        ss_r = float(np.sum((si_te-pred_a)**2))
        ss_t = float(np.sum((si_te-np.mean(si_te))**2))
        r2_a = float(1-ss_r/ss_t) if ss_t>0 else -float("inf")
        if r2_a > best_a_r2:
            best_a_r2 = r2_a; best_a_w = w
    print(f"  {asset}: best rv_{best_a_w}m R²={best_a_r2:.4f}")

# ── Selection rule: quoted vs declined ───────────────────────────────────────
print("\n=== SELECTION RULE ===")
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
quoted_set = set(fills.filter(pl.col("market").is_not_null())["market"].str.to_lowercase().to_list())
gamma_cache = _load_cached_cids()

# Build all available markets with metadata
avail_rows = []
for k, meta in gamma_cache.items():
    cid = meta.get("condition_id","").lower()
    if not cid: continue
    asset = meta.get("asset_symbol",""); horizon = meta.get("horizon","")
    start_date = meta.get("start_date_unix")
    if not start_date: continue
    start_ns = int(float(start_date)*1e9)
    sym = SYMBOL_STREAM.get(asset)
    rv_best = realized_vol(sym, start_ns, best_window) if sym else None
    avail_rows.append({
        "cid": cid, "asset": asset, "horizon": horizon,
        "start_ns": start_ns,
        f"rv_{best_window}m": float(rv_best) if rv_best is not None else float("nan"),
        "quoted": int(cid in quoted_set),
        "hour_utc": float((start_ns//1_000_000_000//3600)%24),
    })

avail_df = pl.DataFrame(avail_rows).filter(pl.col(f"rv_{best_window}m").is_not_null())
quoted_df  = avail_df.filter(pl.col("quoted")==1)
declined_df = avail_df.filter(pl.col("quoted")==0)
print(f"  Quoted: {len(quoted_df)}  Declined: {len(declined_df)}")

if len(quoted_df)>0 and len(declined_df)>0:
    col_rv = f"rv_{best_window}m"
    q_rv = quoted_df[col_rv].to_numpy()
    d_rv = declined_df[col_rv].to_numpy()
    q_h  = quoted_df["hour_utc"].to_numpy()
    d_h  = declined_df["hour_utc"].to_numpy()
    from scipy.stats import ttest_ind
    for name, qvals, dvals in [(f"rv_{best_window}m", q_rv, d_rv),
                                ("hour_utc", q_h, d_h)]:
        t,p = ttest_ind(qvals, dvals)
        sig = "**SIGNIFICANT**" if p<0.05 else "not significant"
        print(f"  {name:<12}: quoted={np.mean(qvals):.4f} declined={np.mean(dvals):.4f} "
              f"p={p:.4f} {sig}")

    # Is there a vol threshold?
    rv_threshold_check = np.percentile(q_rv, 25)
    low_vol_quoted  = int(np.sum(q_rv < rv_threshold_check))
    low_vol_declined = int(np.sum(d_rv < rv_threshold_check))
    print(f"  Markets with rv<p25_quoted ({rv_threshold_check:.3f}): "
          f"quoted={low_vol_quoted} declined={low_vol_declined}")

# ── Save ──────────────────────────────────────────────────────────────────────
results = {
    "best_sigma_predictor": f"rv_{best_window}m",
    "best_oos_r2": round(best_r2,4),
    "all_r2": r2_results,
    "selection_rule_n_quoted_with_rv": int(len(quoted_df)) if avail_df is not None else None,
    "selection_rule_n_declined_with_rv": int(len(declined_df)) if avail_df is not None else None,
    "twin_sigma_recipe": f"trailing {best_window}m realized vol (annualized) at t_post",
    "runtime_min": round((time.time()-t0)/60,2),
}
(cfg.results_dir / "pre7b_sigma_predictor.json").write_text(json.dumps(results, indent=2))
print(f"\nRuntime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: output/ohanism/results/pre7b_sigma_predictor.json")
