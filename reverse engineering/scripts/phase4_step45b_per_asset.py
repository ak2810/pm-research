"""Phase 4 Step 4.5b: Per-asset residual diagnostic from pooled L2 fit.

Uses Stage 1 σ-recipe (ewma_94=0.74, ewma_90=0.22, ewma_97=0.03) and Stage 2b
spread params as best available θ̂. Computes residuals e_i = p_obs - p_model
per asset. Tests whether ē_asset differs significantly from 0 (Bonferroni α=0.01).
"""
import sys
import json
import warnings

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from scipy.optimize import minimize
from scipy.stats import norm, t as tdist

warnings.filterwarnings("ignore")

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()

# ── Load L2 results + data ─────────────────────────────────────────────────────
l2_res = json.loads((cfg.results_dir / "phase4_l2.json").read_text())
# Best θ̂: Stage 1 σ-recipe + Stage 2b spread params (per BLOCKER-005 recommendation)
W_STAGE1 = np.array([
    l2_res["stage1"]["weights"]["ewma_90"],
    l2_res["stage1"]["weights"]["ewma_94"],
    l2_res["stage1"]["weights"]["ewma_97"],
    l2_res["stage1"]["weights"]["rv_1m"],
    l2_res["stage1"]["weights"]["rv_5m"],
    l2_res["stage1"]["weights"]["park_1h"],
    l2_res["stage1"]["weights"]["seasonal"],
])
THETA_H = l2_res["stage2b"]["theta_h"]
THETA_RHO = l2_res["stage2b"]["theta_rho"]
THETA_C = l2_res["stage2b"]["theta_c"]
EST_NAMES = ["ewma_90","ewma_94","ewma_97","rv_1m","rv_5m","park_1h","seasonal"]

print(f"θ̂ σ-recipe (Stage 1): " + " ".join(f"{n}={v:.3f}" for n,v in zip(EST_NAMES, W_STAGE1)))
print(f"θ̂ spread (Stage 2b): θ_h={THETA_H} θ_ρ={THETA_RHO:.4f} θ_c={THETA_C}")

SECS_PER_YEAR = 365.25 * 24 * 3600
SYMBOL_STREAM = {"BTC":"btcusdt","ETH":"ethusdt","SOL":"solusdt","XRP":"xrpusdt","DOGE":"dogeusdt"}
DATES = ["2026-05-27","2026-05-28","2026-05-29"]

# ── Reload dataset (same as Step 4.5) ─────────────────────────────────────────
sig_v2_raw = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fill_sides = (
    fills.filter(pl.col("market").is_not_null() & pl.col("t_ws_ns").is_not_null())
    .sort("t_block_ns").group_by("market")
    .agg([pl.first("ohanism_side").alias("ohanism_side"),
          pl.first("outcome_side").alias("outcome_side")])
    .rename({"market": "market_id"})
)
sig_v2 = sig_v2_raw.join(fill_sides, on="market_id", how="left")
sig_v2 = sig_v2.sort("sigma_implied").unique(subset=["market_id"], keep="first")

# Build estimators at t_post_ns (reuse phase4_step45 logic)
import json as _json, time
est_rows = []
for asset, stream in SYMBOL_STREAM.items():
    asset_mkts = sig_v2.filter(pl.col("asset_symbol") == asset)
    if asset_mkts.is_empty(): continue
    ticker_rows, kline_rows = [], []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e","s","b","a","t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).with_columns(
                ((pl.col("b").cast(pl.Float64)+pl.col("a").cast(pl.Float64))/2.0).alias("mid")
            ).select(["t_recv_ns","mid"]).collect()
            if len(df): ticker_rows.append(df)
        except FileNotFoundError: pass
        try:
            lf_k = scan_feed("binance", date, columns=["e","s","k","t_recv_ns"])
            df_k = lf_k.filter(
                (pl.col("e")=="kline") & (pl.col("s").str.to_lowercase()==stream)
                & pl.col("k").is_not_null()
            ).collect()
            if len(df_k): kline_rows.append(df_k)
        except FileNotFoundError: pass

    if not ticker_rows: continue
    ticker = pl.concat(ticker_rows).sort("t_recv_ns")
    mids = ticker["mid"].to_numpy(); ts_ns = ticker["t_recv_ns"].to_numpy()
    log_ret = np.diff(np.log(mids))
    ts_min = (ts_ns//(60*1_000_000_000)).astype(np.int64)
    unique_mins = np.unique(ts_min)
    min_prices = np.array([mids[np.where(ts_min==m)[0][-1]] for m in unique_mins])
    min_rets = np.diff(np.log(min_prices))
    klines_df = None
    if kline_rows:
        kp = []
        for df_k in kline_rows:
            for row in df_k.iter_rows(named=True):
                try:
                    k = _json.loads(row["k"])
                    kp.append({"t_open_ms":int(k["t"]),"high":float(k["h"]),"low":float(k["l"])})
                except: pass
        if kp: klines_df = pl.DataFrame(kp).sort("t_open_ms")

    for row in asset_mkts.iter_rows(named=True):
        t_q = row["t_post_ns"]; mkt = row["market_id"]
        idx_q = min(int(np.searchsorted(ts_ns, t_q)), len(ts_ns)-1)
        t_q_min = int(t_q//(60*1_000_000_000))
        idx_min_q = min(int(np.searchsorted(unique_mins, t_q_min)), len(min_rets))
        rec = {"market_id": mkt}
        for Wm, wn in [(1,"rv_1m"),(5,"rv_5m")]:
            si = max(0, idx_q-max(1,int(Wm*60/0.1))); rw = log_ret[si:min(idx_q,len(log_ret))]
            if len(rw)<2: rec[wn]=np.nan; continue
            dt_s=(ts_ns[idx_q]-ts_ns[si])/1e9
            rec[wn] = float(np.sqrt(np.mean(rw**2)/(dt_s/len(rw))*SECS_PER_YEAR)) if dt_s>0 else np.nan
        for lam, nm in [(0.90,"ewma_90"),(0.94,"ewma_94"),(0.97,"ewma_97")]:
            r1m = min_rets[max(0,idx_min_q-1440):idx_min_q]
            if len(r1m)<5: rec[nm]=np.nan; continue
            h = float(np.var(r1m[:10]) if len(r1m)>=10 else r1m[0]**2)
            for r in r1m: h = lam*h+(1-lam)*float(r)**2
            rec[nm] = float(np.sqrt(h*1440*365.25))
        if klines_df is not None:
            idx_k = int(np.searchsorted(klines_df["t_open_ms"].to_numpy(), t_q//1_000_000))
            sub = klines_df.slice(max(0,idx_k-60), min(60,idx_k))
            if len(sub)>=5:
                H=sub["high"].to_numpy(); L=sub["low"].to_numpy()
                rec["park_1h"] = float(np.sqrt(np.mean(np.log(H/L)**2)/(4*np.log(2))*1440*365.25))
            else: rec["park_1h"] = np.nan
        else: rec["park_1h"] = np.nan
        W_t60 = max(1,int(60*60/0.1)); si60 = max(0,idx_q-W_t60)
        rw60 = log_ret[si60:min(idx_q,len(log_ret))]
        dt60 = (ts_ns[idx_q]-ts_ns[si60])/1e9 if si60 < idx_q else 0
        rec["seasonal"] = float(np.sqrt(np.mean(rw60**2)/(dt60/len(rw60))*SECS_PER_YEAR)) if len(rw60)>=2 and dt60>0 else np.nan
        est_rows.append(rec)

est_df = pl.DataFrame(est_rows)
df = sig_v2.join(est_df, on="market_id", how="inner")
for col in EST_NAMES: df = df.filter(pl.col(col).is_not_null() & pl.col(col).is_finite())
df = df.filter(pl.col("sigma_implied").is_not_null() & (pl.col("sigma_implied") > 0))

p_obs = df["p_posted"].to_numpy()
S0 = df["S0"].to_numpy(); St = df["S_t"].to_numpy(); tau_y = df["tau_years"].to_numpy()
est_mat = df.select(EST_NAMES).to_numpy()
os_arr = df["outcome_side"].to_numpy() if "outcome_side" in df.columns else np.full(len(df), "Up")
ohs_arr = df["ohanism_side"].to_numpy() if "ohanism_side" in df.columns else np.full(len(df), "SELL")
direction = np.where(
    ((ohs_arr=="SELL")&(os_arr=="Up")) | ((ohs_arr=="BUY")&(os_arr=="Down")),
    1.0, -1.0
)
asset_arr = df["asset_symbol"].to_numpy()

# Compute p_model using best θ̂
sig = est_mat @ W_STAGE1
fv = 1.0 - norm.cdf(np.log(S0/St) / (np.maximum(sig, 1e-6) * np.sqrt(tau_y)))
hs = THETA_H[0] + THETA_H[1] * sig * np.sqrt(tau_y)
reb = THETA_RHO * (0.5 - np.minimum(fv, 1.0-fv))
otm = THETA_C[0] + THETA_C[1] * sig * np.sqrt(tau_y)
p_model = np.clip(fv - direction * hs + reb + otm, 0.01, 0.99)
residuals = p_obs - p_model

N = len(df)
print(f"\nN={N}, overall RMSE={np.sqrt(np.mean(residuals**2)):.4f}")

# ── Per-asset residual analysis ────────────────────────────────────────────────
print("\n=== PER-ASSET RESIDUALS (Bonferroni α=0.01 across 5 assets) ===")
BONFERRONI_P = 0.01  # 0.05 / 5 assets
results_45b = {}

for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    mask = asset_arr == asset
    n_a = mask.sum()
    if n_a < 5:
        print(f"  {asset}: n={n_a} (too few)")
        results_45b[asset] = {"n": int(n_a), "mean_resid": None, "significant": False}
        continue
    e_a = residuals[mask]
    mean_e = float(np.mean(e_a))
    # HAC variance of the mean (Newey-West, lag = round(4*(n/100)^(2/9)))
    from statsmodels.stats.sandwich_covariance import cov_hac  # type: ignore[import-untyped]
    import statsmodels.api as sm
    lm = sm.OLS(e_a, np.ones(n_a)).fit(cov_type="HAC",
                                         cov_kwds={"maxlags": max(1, round(4*(n_a/100)**(2/9)))})
    se_hac = float(lm.bse[0])
    t_stat = mean_e / se_hac if se_hac > 0 else 0.0
    pval = float(2 * (1 - tdist.cdf(abs(t_stat), df=n_a-1)))
    significant = pval < BONFERRONI_P

    med_e = float(np.median(e_a))
    p25_e = float(np.percentile(e_a, 25))
    p75_e = float(np.percentile(e_a, 75))
    rmse_a = float(np.sqrt(np.mean(e_a**2)))

    flag = "⚠ SIGNIFICANT" if significant else "OK"
    print(f"  {asset}: n={n_a} ē={mean_e:+.4f} SE_HAC={se_hac:.4f} t={t_stat:.2f} p={pval:.4f} "
          f"RMSE={rmse_a:.4f} [{flag}]")
    print(f"         p25={p25_e:+.4f} median={med_e:+.4f} p75={p75_e:+.4f}")

    results_45b[asset] = {"n": int(n_a), "mean_resid": round(mean_e, 5),
                          "se_hac": round(se_hac, 5), "t_stat": round(t_stat, 3),
                          "pval": round(pval, 4), "significant": significant,
                          "rmse": round(rmse_a, 5)}

# ── Decision rule ──────────────────────────────────────────────────────────────
sig_assets = [a for a, r in results_45b.items() if r.get("significant")]
print(f"\n=== DECISION RULE ===")
if not sig_assets:
    print("No asset has ē significantly ≠ 0. ONE pooled σ recipe fits all 5 assets.")
    print("→ Proceed to profitability decomposition (Step 4.6) with pooled model.")
    decision = "pooled"
elif sig_assets == ["XRP"] or sig_assets == ["XRP", "DOGE"] or sig_assets == ["DOGE"]:
    print(f"Only {sig_assets} significant → refit separately per Phase 4 spec.")
    print("→ Refit those assets with separate θ_σ. Pooled model for the rest.")
    decision = f"separate:{','.join(sig_assets)}"
else:
    print(f"{len(sig_assets)} assets significant {sig_assets} → pooled model mis-specified.")
    print("→ Fit each asset separately. Report per-asset θ_σ.")
    decision = f"per_asset:{','.join(sig_assets)}"
print(f"Decision: {decision}")

# Save
full_results = {"N": int(N), "theta_hat": {"sigma_recipe": W_STAGE1.tolist(),
                "theta_h": THETA_H, "theta_rho": THETA_RHO, "theta_c": THETA_C},
                "per_asset": results_45b, "decision": decision,
                "overall_rmse": float(np.sqrt(np.mean(residuals**2)))}
(cfg.results_dir / "phase4_per_asset.json").write_text(_json.dumps(full_results, indent=2))
print("\nSaved: output/results/phase4_per_asset.json")
