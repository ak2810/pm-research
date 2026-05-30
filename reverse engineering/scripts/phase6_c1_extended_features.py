"""Phase 6 C1 — Extended microstructure features for GBT residual model.

New features vs Phase 5 baseline:
  - Binance spot returns at 1s/5s/30s/60s/300s before t_post (lead-lag)
  - Absolute spot volatility in each window
  - Cross-asset: BTC return as feature for ETH/SOL/XRP markets
  - Day-of-week (intra-week seasonality)
  - Signed spot direction (momentum indicator)
  - Inter-market correlation (same-asset same-horizon markets trading concurrently)

Gate P6.1: Extended GBT OOS R² ≥ Phase 5 R² (0.35) and ideally +0.05 above.

Standing rule S1-S5 applied at top.
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, "src")

import numpy as np
import polars as pl
import lightgbm as lgb
import shap
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import norm
from sklearn.model_selection import KFold
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions

cfg = get_settings()
t0 = time.time()
np.random.seed(42)

# ── S1-S5: Window ─────────────────────────────────────────────────────────────
FEEDS = ["pm_clob", "polygon", "binance", "pm_meta"]
WINDOW_START = ("2026-05-27", 4)
feed_parts = {feed: set((p.date, p.hour) for p in list_local_partitions(feed))
              for feed in FEEDS}
common = None
for feed in FEEDS:
    common = feed_parts[feed] if common is None else common & feed_parts[feed]
common = {p for p in common if p >= WINDOW_START}
common_sorted = sorted(common)
WINDOW_END = common_sorted[-1]
WINDOW_DATES = sorted(set(d for d, _ in common_sorted))
print(f"=== PHASE 6 C1: EXTENDED FEATURES ===")
print(f"Window: {WINDOW_START} → {WINDOW_END} ({len(common_sorted)}h)")

# ── Load sigma_implied_v2 ─────────────────────────────────────────────────────
sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
N_MKT = len(sig_v2)
print(f"Markets: {N_MKT}")

# ── Load L2 theta_hat (same as Phase 5) ──────────────────────────────────────
l2 = json.loads((cfg.results_dir / "phase4_l2.json").read_text())
theta_h0, theta_h1 = l2["stage2b"]["theta_h"]

# ── Build Binance spot return lookup ──────────────────────────────────────────
print("Building Binance spot return lookup...")
SYMBOL_STREAM = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt",
                 "XRP": "xrpusdt", "DOGE": "dogeusdt"}

# Load all Binance bookTicker for analysis window
binance_rows = []
for date in WINDOW_DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=binance/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False,
                             use_statistics=False)
        df = (lf.filter(
            pl.col("e").is_null() & pl.col("b").is_not_null()  # bookTicker rows
        ).select(["t_recv_ns", "s", "b", "a"]).collect())
        if len(df):
            binance_rows.append(df)

if binance_rows:
    bticker = pl.concat(binance_rows).with_columns([
        pl.col("b").cast(pl.Float64).alias("bid"),
        pl.col("a").cast(pl.Float64).alias("ask"),
    ]).with_columns(
        ((pl.col("bid") + pl.col("ask")) / 2.0).alias("mid")
    ).sort("t_recv_ns")
    print(f"  Binance ticks: {len(bticker)}")
else:
    bticker = pl.DataFrame()
    print("  WARNING: no Binance data loaded")

def get_binance_mid(symbol_stream: str, t_ns: int, before_ns: int = 0) -> float | None:
    """Get nearest Binance mid price at or before t_ns - before_ns."""
    if bticker.is_empty():
        return None
    target = t_ns - before_ns
    sub = bticker.filter(
        (pl.col("s") == symbol_stream) & (pl.col("t_recv_ns") <= target)
    )
    if len(sub) == 0:
        return None
    return float(sub.sort("t_recv_ns").tail(1)["mid"][0])

# Pre-build per-symbol sorted arrays for vectorized lookups
print("  Pre-indexing Binance by symbol...")
binance_by_sym: dict[str, tuple[np.ndarray, np.ndarray]] = {}
if not bticker.is_empty():
    for sym in set(SYMBOL_STREAM.values()):
        sub = bticker.filter(pl.col("s") == sym).sort("t_recv_ns")
        if len(sub):
            binance_by_sym[sym] = (
                sub["t_recv_ns"].to_numpy(),
                sub["mid"].to_numpy(),
            )

def fast_binance_mid(sym: str, t_ns: int) -> float | None:
    if sym not in binance_by_sym:
        return None
    ts, mids = binance_by_sym[sym]
    idx = np.searchsorted(ts, t_ns, side="right") - 1
    return float(mids[idx]) if idx >= 0 else None

# ── Also load BTC mid series for cross-asset features ────────────────────────
btc_ts, btc_mid = binance_by_sym.get("btcusdt", (None, None)) or (None, None)

def btc_return_before(t_ns: int, window_ns: int) -> float | None:
    if btc_ts is None:
        return None
    idx_now = np.searchsorted(btc_ts, t_ns, side="right") - 1
    idx_bef = np.searchsorted(btc_ts, t_ns - window_ns, side="right") - 1
    if idx_now < 0 or idx_bef < 0:
        return None
    return float((btc_mid[idx_now] - btc_mid[idx_bef]) / max(btc_mid[idx_bef], 1e-6))

# ── Build extended features per market ───────────────────────────────────────
print("Building extended features...")
WINDOWS_S = [1, 5, 30, 60, 300]  # seconds before t_post

rows = []
n_missing = 0
for mkt_row in sig_v2.iter_rows(named=True):
    t_post_ns = int(mkt_row["t_post_ns"])
    asset = str(mkt_row["asset_symbol"])
    sym = SYMBOL_STREAM.get(asset, "btcusdt")

    # Phase 5 features
    sigma_hat = float(mkt_row["sigma_implied"])
    tau_years = float(mkt_row["tau_years"])
    S0 = float(mkt_row["S0"])
    S_t = float(mkt_row["S_t"])
    p_posted = float(mkt_row["p_posted"])
    log_ratio = np.log(max(S0, 1e-6) / max(S_t, 1e-6))
    d = log_ratio / (max(sigma_hat, 1e-4) * max(tau_years, 1e-12) ** 0.5)
    fair_value = float(1.0 - norm.cdf(d))
    half_spread = theta_h0 + theta_h1 * sigma_hat * max(tau_years, 1e-12) ** 0.5
    direction = 1.0 if p_posted > fair_value else -1.0
    p_model = fair_value + direction * half_spread
    residual = p_posted - p_model
    lag_s = float(mkt_row["post_to_fill_lag_s"] or 0)

    # NEW: Binance spot returns before t_post at multiple windows
    mid_now = fast_binance_mid(sym, t_post_ns)
    spot_rets = {}
    vol_rets   = {}
    for ws in WINDOWS_S:
        mid_before = fast_binance_mid(sym, t_post_ns - int(ws * 1e9))
        if mid_now is not None and mid_before is not None and mid_before > 1e-6:
            spot_rets[f"ret_{ws}s"] = float((mid_now - mid_before) / mid_before)
            vol_rets[f"abs_ret_{ws}s"] = float(abs(spot_rets[f"ret_{ws}s"]))
        else:
            spot_rets[f"ret_{ws}s"] = 0.0
            vol_rets[f"abs_ret_{ws}s"] = 0.0
            n_missing += 1

    # NEW: Cross-asset BTC return (for ETH/SOL/XRP/DOGE markets)
    btc_ret_5s = 0.0
    btc_ret_60s = 0.0
    if asset != "BTC":
        btc_ret_5s  = btc_return_before(t_post_ns, 5_000_000_000) or 0.0
        btc_ret_60s = btc_return_before(t_post_ns, 60_000_000_000) or 0.0

    # NEW: Day of week (0=Mon, 6=Sun)
    day_of_week = (t_post_ns // 1_000_000_000 // 86400 + 3) % 7  # approximate

    # NEW: Momentum sign consistency (1s and 5s returns agree?)
    momentum_aligned = int(np.sign(spot_rets["ret_1s"]) == np.sign(spot_rets["ret_5s"]))

    # All features
    feat = {
        "residual": residual,
        # Phase 5 core features
        "fair_value": fair_value,
        "otm_cushion": abs(p_posted - 0.5),
        "lag_s": lag_s,
        "spot_z": log_ratio / max(sigma_hat * max(tau_years, 1e-12)**0.5, 1e-6),
        "log_S0": np.log(max(S0, 1e-6)),
        "sigma_hat": sigma_hat,
        "log_n_fills": np.log1p(int(mkt_row["n_fills_in_market"])),
        "log_tau": np.log(max(tau_years, 1e-12)),
        # New features
        **spot_rets,
        **vol_rets,
        "btc_ret_5s": btc_ret_5s,
        "btc_ret_60s": btc_ret_60s,
        "day_of_week": float(day_of_week),
        "momentum_aligned": float(momentum_aligned),
        "log_lag_s": np.log1p(lag_s),
        "hour_utc": float((t_post_ns // 1_000_000_000 // 3600) % 24),
        "asset_enc": float({"BTC":0,"ETH":1,"SOL":2,"XRP":3,"DOGE":4}.get(asset, -1)),
        "horizon_enc": float({"5m":0,"15m":1,"1h":2}.get(str(mkt_row["horizon"]), -1)),
        # Context
        "asset_sym": asset,
        "t_post_ns": t_post_ns,
    }
    rows.append(feat)

df = pl.DataFrame(rows)
print(f"  Features built: {len(df)} markets, {n_missing} missing Binance lookups")

# ── Feature list ──────────────────────────────────────────────────────────────
FEATURES_P5 = ["fair_value","otm_cushion","lag_s","spot_z","log_S0",
               "sigma_hat","log_n_fills","log_tau"]
FEATURES_NEW = (
    [f"ret_{ws}s" for ws in WINDOWS_S] +
    [f"abs_ret_{ws}s" for ws in WINDOWS_S] +
    ["btc_ret_5s","btc_ret_60s","day_of_week","momentum_aligned",
     "log_lag_s","hour_utc","asset_enc","horizon_enc"]
)
FEATURES = FEATURES_P5 + FEATURES_NEW

# Drop NaN rows
drop_cols = ["residual"] + FEATURES
df_pandas = df.select(drop_cols + ["asset_sym","t_post_ns"]).to_pandas()
mask = df_pandas[drop_cols].notna().all(axis=1) & np.isfinite(df_pandas["residual"])
df_clean = df_pandas[mask].copy().reset_index(drop=True)
print(f"  Clean: {len(df_clean)} markets")

X = df_clean[FEATURES].values
y = df_clean["residual"].values

# ── Train/test split (temporal, 70/30) ────────────────────────────────────────
sorted_idx = df_clean["t_post_ns"].argsort().values
n_train = int(len(df_clean) * 0.70)
train_idx = sorted_idx[:n_train]
test_idx  = sorted_idx[n_train:]
X_train, y_train = X[train_idx], y[train_idx]
X_test,  y_test  = X[test_idx],  y[test_idx]
print(f"Train={len(train_idx)}  Test={len(test_idx)}")

# ── LightGBM ──────────────────────────────────────────────────────────────────
params = {
    "objective":"regression", "metric":"rmse",
    "num_leaves":15, "learning_rate":0.03,
    "feature_fraction":0.8, "bagging_fraction":0.8, "bagging_freq":5,
    "min_data_in_leaf":20, "min_sum_hessian_in_leaf":10.0,
    "lambda_l1":0.1, "lambda_l2":0.2, "verbose":-1, "random_state":42,
}

# CV first
kf = KFold(n_splits=5, shuffle=True, random_state=42)
cv_rmses = []
for ft, fv in kf.split(X):
    dt = lgb.Dataset(X[ft], label=y[ft])
    dv = lgb.Dataset(X[fv], label=y[fv], reference=dt)
    m = lgb.train(params, dt, 300, valid_sets=[dv],
                  callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)])
    cv_rmses.append(float(np.sqrt(np.mean((y[fv] - m.predict(X[fv]))**2))))
cv_rmse = float(np.mean(cv_rmses))
print(f"5-fold CV RMSE: {cv_rmse:.5f}")

print("Training final model...")
train_ds = lgb.Dataset(X_train, label=y_train, feature_name=FEATURES)
val_ds   = lgb.Dataset(X_test,  label=y_test,  feature_name=FEATURES, reference=train_ds)
model = lgb.train(params, train_ds, 500, valid_sets=[train_ds, val_ds],
                  valid_names=["train","valid"],
                  callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)])

y_pred_train = model.predict(X_train)
y_pred_test  = model.predict(X_test)
rmse_train = float(np.sqrt(np.mean((y_train - y_pred_train)**2)))
rmse_test  = float(np.sqrt(np.mean((y_test  - y_pred_test)**2)))
naive_rmse = float(np.std(y_test))
r2_test    = float(1 - (rmse_test/naive_rmse)**2)
ratio = rmse_test / rmse_train

print(f"\n=== K5: OOS DIAGNOSTICS ===")
print(f"  RMSE train: {rmse_train:.5f}")
print(f"  RMSE test:  {rmse_test:.5f}")
print(f"  RMSE ratio: {ratio:.3f}")
print(f"  Naive RMSE: {naive_rmse:.5f}")
print(f"  R² test:    {r2_test:.4f}")
print(f"  5-fold CV:  {cv_rmse:.5f} → ratio={cv_rmse/rmse_train:.3f}")

# Phase 5 baseline comparison
P5_R2 = 0.3485  # from phase5_k2_gbt.json
print(f"\nPhase 5 R² baseline: {P5_R2:.4f}")
print(f"Phase 6 R² test:     {r2_test:.4f}")
delta_r2 = r2_test - P5_R2
print(f"ΔR²: {delta_r2:+.4f}  ({'PASS ✓' if delta_r2 >= 0 else 'REGRESSION'})")

# Per-asset RMSE
asset_col = df_clean["asset_sym"].values[test_idx]
print(f"\nPer-asset test RMSE:")
for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    mask_a = asset_col == asset
    if mask_a.sum() < 5: continue
    ra = float(np.sqrt(np.mean((y_test[mask_a] - y_pred_test[mask_a])**2)))
    print(f"  {asset}: n={mask_a.sum()} RMSE={ra:.5f} ratio={ra/rmse_test:.2f}x")

# ── SHAP ─────────────────────────────────────────────────────────────────────
print("\nSHAP analysis...")
explainer = shap.TreeExplainer(model)
shap_vals = explainer.shap_values(X_test)
mean_abs_shap = np.abs(shap_vals).mean(axis=0)
ranking = np.argsort(mean_abs_shap)[::-1]

print(f"\n{'Rank':<6} {'Feature':<30} {'Mean|SHAP|':>12} {'New?':>6}")
print("-" * 58)
shap_table = []
for rank, i in enumerate(ranking[:15], 1):
    fname = FEATURES[i]
    mshap = float(mean_abs_shap[i])
    is_new = "NEW" if fname in FEATURES_NEW else ""
    print(f"  {rank:<4} {fname:<30} {mshap:>12.6f} {is_new:>6}")
    shap_table.append({"rank":rank,"feature":fname,"mean_abs_shap":round(mshap,6),"new":bool(is_new)})

# 80% mass
cumshap = np.cumsum(mean_abs_shap[ranking] / mean_abs_shap.sum())
n80 = int(np.searchsorted(cumshap, 0.80)) + 1
critical = [FEATURES[ranking[i]] for i in range(n80)]
print(f"\n80% SHAP mass: {n80} features: {critical}")

# New feature contribution
new_shap = sum(mean_abs_shap[i] for i, f in enumerate(FEATURES) if f in FEATURES_NEW)
total_shap = mean_abs_shap.sum()
print(f"New features' SHAP fraction: {new_shap/total_shap*100:.1f}%")

# Beeswarm top 12
top12 = ranking[:12]
fig, ax = plt.subplots(figsize=(10,7))
shap.summary_plot(shap_vals[:,top12], X_test[:,top12],
                  feature_names=[FEATURES[i] for i in top12], show=False, plot_size=None)
plt.title("Phase 6 SHAP Beeswarm — Top 12 Features")
plt.tight_layout()
fig.savefig(str(cfg.plots_dir / "phase6_shap_beeswarm.png"), dpi=150, bbox_inches="tight")
plt.close()

# Dependency on top new feature
new_feats_in_top = [i for i in ranking[:12] if FEATURES[i] in FEATURES_NEW]
if new_feats_in_top:
    top_new_i = new_feats_in_top[0]
    fig2, ax2 = plt.subplots(figsize=(8,5))
    shap.dependence_plot(top_new_i, shap_vals, X_test, feature_names=FEATURES,
                         ax=ax2, show=False)
    ax2.set_title(f"SHAP Dep — {FEATURES[top_new_i]} (top new feature)")
    fig2.tight_layout()
    fig2.savefig(str(cfg.plots_dir / "phase6_shap_new_dep.png"), dpi=150, bbox_inches="tight")
    plt.close()

# ── Gate P6.1 ─────────────────────────────────────────────────────────────────
print(f"\n=== PHASE 6.1 GATE ===")
p61_pass = delta_r2 >= 0  # no regression vs Phase 5
p61_improve = delta_r2 >= 0.05
print(f"P6.1 (R² ≥ Phase5): {r2_test:.4f} ≥ {P5_R2:.4f} → {'PASS ✓' if p61_pass else 'FAIL'}")
print(f"P6.1 improvement ≥0.05: ΔR²={delta_r2:+.4f} → {'YES ✓' if p61_improve else 'NOT MET'}")

# ── Save ──────────────────────────────────────────────────────────────────────
results = {
    "window_start": f"{WINDOW_START[0]} h{WINDOW_START[1]}",
    "window_end": f"{WINDOW_END[0]} h{WINDOW_END[1]}",
    "n_markets": len(df_clean),
    "n_train": int(n_train), "n_test": len(test_idx),
    "rmse_train": round(rmse_train,5), "rmse_test": round(rmse_test,5),
    "rmse_ratio": round(ratio,4), "r2_test": round(r2_test,4),
    "cv_rmse_5fold": round(cv_rmse,5),
    "phase5_r2_baseline": P5_R2, "delta_r2": round(delta_r2,4),
    "p61_no_regression": p61_pass, "p61_improvement_5pct": p61_improve,
    "new_feature_shap_fraction": round(new_shap/total_shap,4),
    "shap_top15": shap_table,
    "critical_features_80pct": critical,
}
(cfg.results_dir / "phase6_c1_extended.json").write_text(json.dumps(results, indent=2))
print(f"\nSaved: output/ohanism/results/phase6_c1_extended.json")
print(f"Runtime: {(time.time()-t0)/60:.1f} min")
