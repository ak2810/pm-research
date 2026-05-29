"""Pre-5.B: Profile likelihood over EWMA λ to diagnose BLOCKER-005.

For λ ∈ {0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98}:
  1. Fix σ̂ = σ_ewma_λ (single estimator)
  2. Refit θ_h, θ_ρ, θ_c
  3. Record log-likelihood

Then find λ_MLE and 95% CI (ΔlogL ≤ 1.92).
"""
import sys
import json
import time
import warnings

sys.path.insert(0, "src")

import numpy as np
import polars as pl
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.stats import norm

warnings.filterwarnings("ignore")

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
SECS_PER_YEAR = 365.25 * 24 * 3600
DATES = ["2026-05-27","2026-05-28","2026-05-29"]
SYMBOL_STREAM = {"BTC":"btcusdt","ETH":"ethusdt","SOL":"solusdt","XRP":"xrpusdt","DOGE":"dogeusdt"}

t0 = time.time()

# ── Load L2 dataset ────────────────────────────────────────────────────────────
sig_v2_raw = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fill_sides = (
    fills.filter(pl.col("market").is_not_null() & pl.col("t_ws_ns").is_not_null())
    .sort("t_block_ns").group_by("market")
    .agg([pl.first("ohanism_side").alias("ohanism_side"),
          pl.first("outcome_side").alias("outcome_side")])
    .rename({"market":"market_id"})
)
sig_v2 = sig_v2_raw.join(fill_sides, on="market_id", how="left")
sig_v2 = sig_v2.sort("sigma_implied").unique(subset=["market_id"], keep="first")

print(f"L2 dataset: {len(sig_v2)} markets")

# ── Build per-market EWMA estimates at t_post_ns ──────────────────────────────
print("Building EWMA estimates for all λ values...")
LAMBDAS = [0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98]
ewma_by_lambda: dict[float, dict[str, float]] = {lam: {} for lam in LAMBDAS}
# keys: market_id → σ_ewma_λ value

for asset, stream in SYMBOL_STREAM.items():
    asset_mkts = sig_v2.filter(pl.col("asset_symbol") == asset)
    if asset_mkts.is_empty(): continue

    ticker_rows = []
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

    if not ticker_rows: continue
    ticker = pl.concat(ticker_rows).sort("t_recv_ns")
    ts_ns = ticker["t_recv_ns"].to_numpy()
    ts_min = (ts_ns // (60 * 1_000_000_000)).astype(np.int64)
    unique_mins = np.unique(ts_min)
    mids = ticker["mid"].to_numpy()
    min_prices = np.array([mids[np.where(ts_min==m)[0][-1]] for m in unique_mins])
    min_rets = np.diff(np.log(min_prices))

    for row in asset_mkts.iter_rows(named=True):
        t_q = row["t_post_ns"]; mkt = row["market_id"]
        t_q_min = int(t_q // (60 * 1_000_000_000))
        idx_min_q = min(int(np.searchsorted(unique_mins, t_q_min)), len(min_rets))
        r1m = min_rets[max(0, idx_min_q-1440): idx_min_q]
        if len(r1m) < 5: continue

        for lam in LAMBDAS:
            h = float(np.var(r1m[:10]) if len(r1m)>=10 else r1m[0]**2)
            for r in r1m: h = lam*h + (1-lam)*float(r)**2
            ewma_by_lambda[lam][mkt] = float(np.sqrt(h * 1440 * 365.25))

print(f"EWMA estimates built for {len(ewma_by_lambda[0.94])} markets across {len(LAMBDAS)} λ values")

# ── Build base arrays ──────────────────────────────────────────────────────────
p_obs  = sig_v2["p_posted"].to_numpy()
S0     = sig_v2["S0"].to_numpy()
St     = sig_v2["S_t"].to_numpy()
tau_y  = sig_v2["tau_years"].to_numpy()
mids_arr = sig_v2["market_id"].to_numpy()
os_arr = sig_v2["outcome_side"].to_numpy() if "outcome_side" in sig_v2.columns else np.full(len(sig_v2), "Up")
ohs_arr = sig_v2["ohanism_side"].to_numpy() if "ohanism_side" in sig_v2.columns else np.full(len(sig_v2), "SELL")
direction = np.where(
    ((ohs_arr=="SELL")&(os_arr=="Up")) | ((ohs_arr=="BUY")&(os_arr=="Down")),
    1.0, -1.0
)

def p_model_lambda(sigma, theta_h, theta_rho, theta_c):
    fv = 1.0 - norm.cdf(np.log(S0/St) / (np.maximum(sigma, 1e-6) * np.sqrt(tau_y)))
    hs = theta_h[0] + theta_h[1] * sigma * np.sqrt(tau_y)
    reb = theta_rho * (0.5 - np.minimum(fv, 1.0-fv))
    otm = theta_c[0] + theta_c[1] * sigma * np.sqrt(tau_y)
    return np.clip(fv - direction*hs + reb + otm, 0.01, 0.99)

def neg_loglik_fixed_lambda(params, sigma):
    th = [params[0], params[1]]; rho = params[2]; tc = [params[3], params[4]]
    pm = p_model_lambda(sigma, th, rho, tc)
    var = pm*(1-pm) + 1e-6
    return float(0.5*np.sum((p_obs-pm)**2/var + np.log(var)))

# ── Profile likelihood ─────────────────────────────────────────────────────────
print("\n=== PROFILE LIKELIHOOD OVER EWMA λ ===")
BOUNDS = [(1e-4,0.1),(0.0,3.0),(0.0,0.1),(0.0,0.40),(-0.5,0.5)]
INIT   = [0.033, 0.51, 0.0, 0.0, 0.0]
rng = np.random.default_rng(42)

profile_results = []
for lam in LAMBDAS:
    # Get σ_ewma_λ for each market in our dataset
    sigma_arr = np.array([ewma_by_lambda[lam].get(m, float("nan")) for m in mids_arr])
    valid = np.isfinite(sigma_arr) & np.isfinite(p_obs) & np.isfinite(S0) & np.isfinite(St) & np.isfinite(tau_y)
    if valid.sum() < 20:
        print(f"  λ={lam}: insufficient data ({valid.sum()} valid)")
        continue

    # Refit all other params holding σ = σ_ewma_λ
    best_nll = float("inf")
    for restart in range(10):
        x0 = np.array(INIT) if restart==0 else INIT + rng.normal(0, 0.02, 5)
        x0 = np.clip(x0, [b[0] for b in BOUNDS], [b[1] for b in BOUNDS])
        # Use only valid rows
        sigma_v = sigma_arr[valid]; p_v = p_obs[valid]
        S0_v = S0[valid]; St_v = St[valid]; tau_v = tau_y[valid]; dir_v = direction[valid]

        # Inline model for subset
        def nll_sub(params):
            th=[params[0],params[1]]; rho=params[2]; tc=[params[3],params[4]]
            fv=1-norm.cdf(np.log(S0_v/St_v)/(np.maximum(sigma_v,1e-6)*np.sqrt(tau_v)))
            hs=th[0]+th[1]*sigma_v*np.sqrt(tau_v)
            reb=rho*(0.5-np.minimum(fv,1-fv))
            otm=tc[0]+tc[1]*sigma_v*np.sqrt(tau_v)
            pm=np.clip(fv-dir_v*hs+reb+otm,0.01,0.99)
            var=pm*(1-pm)+1e-6
            return float(0.5*np.sum((p_v-pm)**2/var+np.log(var)))

        res = minimize(nll_sub, x0, method="L-BFGS-B", bounds=BOUNDS,
                       options={"maxiter":2000,"ftol":1e-10})
        if res.fun < best_nll:
            best_nll = res.fun

    loglik = -best_nll  # log-likelihood (negated)
    profile_results.append({"lambda": lam, "neg_loglik": best_nll, "loglik": loglik, "n_valid": int(valid.sum())})
    print(f"  λ={lam:.2f}: NLL={best_nll:.2f}  logL={loglik:.2f}  n={valid.sum()}")

# ── Find λ_MLE and 95% CI ───────────────────────────────────────────────────────
print("\n=== RESULTS ===")
profile_results.sort(key=lambda x: x["loglik"], reverse=True)
best = profile_results[0]
lambda_mle = best["lambda"]
max_loglik = best["loglik"]
threshold = max_loglik - 1.92  # 95% CI threshold (χ²_{0.95,1}/2)
ci_lambdas = [r["lambda"] for r in profile_results if r["loglik"] >= threshold]

print(f"λ_MLE = {lambda_mle:.2f}  (logL={max_loglik:.2f})")
print(f"95% CI (ΔlogL ≤ 1.92): λ ∈ [{min(ci_lambdas):.2f}, {max(ci_lambdas):.2f}]")
print(f"Full profile:")
for r in sorted(profile_results, key=lambda x: x["lambda"]):
    in_ci = "✓" if r["loglik"] >= threshold else " "
    print(f"  λ={r['lambda']:.2f}: logL={r['loglik']:+.2f} {in_ci}")

# ── Interpretation ──────────────────────────────────────────────────────────────
ci_width = max(ci_lambdas) - min(ci_lambdas)
print(f"\nCI width: {ci_width:.2f}")
if ci_width <= 0.04:
    interp = f"NARROW CI [{min(ci_lambdas):.2f},{max(ci_lambdas):.2f}] — recipe is specifically λ≈{lambda_mle:.2f}. BLOCKER-005 was numerical."
elif ci_width <= 0.10:
    interp = f"MODERATE CI [{min(ci_lambdas):.2f},{max(ci_lambdas):.2f}] — EWMA decay in [{min(ci_lambdas):.2f},{max(ci_lambdas):.2f}]. Use λ={lambda_mle:.2f} by convention."
else:
    interp = f"WIDE CI [{min(ci_lambdas):.2f},{max(ci_lambdas):.2f}] — likelihood flat over EWMA range. Non-identifiability confirmed. Use λ=0.94 by convention."
print(f"INTERPRETATION: {interp}")

# Compare to Stage 1 mixture weights
l2_results = json.loads((cfg.results_dir / "phase4_l2.json").read_text())
s1_weights = l2_results["stage1"]["weights"]
print(f"\nStage 1 dominant λ: ewma_94={s1_weights['ewma_94']:.3f} ewma_90={s1_weights['ewma_90']:.3f}")
if lambda_mle == 0.94 and s1_weights["ewma_94"] > 0.5:
    print("Profile-λ MLE and mixture both point to λ=0.94 → STORY COHERES ✓")
else:
    print(f"Profile-λ MLE={lambda_mle:.2f} vs mixture dominant λ=0.94 → investigate if disagree")

# ── Plot ────────────────────────────────────────────────────────────────────────
cfg.plots_dir.mkdir(exist_ok=True)
lams_sorted = sorted([r["lambda"] for r in profile_results])
logliks_sorted = [next(r["loglik"] for r in profile_results if r["lambda"]==l) for l in lams_sorted]
fig, ax = plt.subplots(figsize=(8,4))
ax.plot(lams_sorted, logliks_sorted, "o-", lw=2)
ax.axhline(threshold, color="r", linestyle="--", label=f"95% CI threshold (logL={threshold:.1f})")
ax.axvline(lambda_mle, color="g", linestyle="--", label=f"λ_MLE={lambda_mle:.2f}")
ax.set_xlabel("EWMA λ")
ax.set_ylabel("Profile log-likelihood")
ax.set_title(f"Profile Likelihood over EWMA λ (CI: [{min(ci_lambdas):.2f},{max(ci_lambdas):.2f}])")
ax.legend()
fig.tight_layout()
plot_path = cfg.plots_dir / "pre5b_profile_likelihood.png"
fig.savefig(str(plot_path), dpi=150)
plt.close(fig)
print(f"\nPlot saved: {plot_path}")

# Save results
res_out = {
    "profile": sorted(profile_results, key=lambda x: x["lambda"]),
    "lambda_mle": lambda_mle,
    "ci_95": [float(min(ci_lambdas)), float(max(ci_lambdas))],
    "ci_width": float(ci_width),
    "interpretation": interp,
    "coheres_with_stage1": bool(lambda_mle in [0.90, 0.94])
}
(cfg.results_dir / "pre5b_profile_likelihood.json").write_text(json.dumps(res_out, indent=2))
print(f"Saved: output/results/pre5b_profile_likelihood.json")
print(f"\nTotal runtime: {(time.time()-t0)/60:.1f} min")
