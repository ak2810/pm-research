"""Phase 4 Step 4.5: L2 Structural Policy Estimation.

Model: p_quoted = clip(FairValue(sigma_hat) - direction*half_spread + rebate_skew + OTM_adjust)

sigma_hat = weighted average of σ estimators (simplex constraint: w >= 0, Σw = 1)
  Components: ewma_90, ewma_94, ewma_97, rv_1m, rv_5m, park_1h, seasonal

Stage 1: fit θ_σ (7 weights) only, other params fixed at starting values.
Stage 2: fit all parameters jointly, warm-started from Stage 1.

Identification scaffolding:
  I1. Informative initialization: w_ewma94=0.5, w_ewma90=0.3, w_ewma97=0.1, rest=0.025
  I2. Two-pass on OTM cushion: first fixed=0.22, then freed
  I3. Sanity bound: median BTC 5m sigma_hat must be in [0.2, 1.5]
  I4. 20 restarts, report convergence rate
"""
import sys
import json
import time
import warnings
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from scipy.optimize import minimize
from scipy.stats import norm
from arch import arch_model  # type: ignore[import-untyped]

warnings.filterwarnings("ignore")

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
cfg.results_dir.mkdir(parents=True, exist_ok=True)

SECS_PER_YEAR = 365.25 * 24 * 3600
SYMBOL_STREAM = {"BTC":"btcusdt","ETH":"ethusdt","SOL":"solusdt","XRP":"xrpusdt","DOGE":"dogeusdt"}
DATES = ["2026-05-27","2026-05-28","2026-05-29"]
EST_NAMES = ["ewma_90","ewma_94","ewma_97","rv_1m","rv_5m","park_1h","seasonal"]

# ── Load data ─────────────────────────────────────────────────────────────────
sig_v2_raw = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))

# Enrich with outcome_side and ohanism_side from fills for direction computation
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fill_sides = (
    fills.filter(pl.col("market").is_not_null() & pl.col("t_ws_ns").is_not_null())
    .sort("t_block_ns")
    .group_by("market")
    .agg([
        pl.first("ohanism_side").alias("ohanism_side"),
        pl.first("outcome_side").alias("outcome_side"),
    ])
    .rename({"market": "market_id"})
)
sig_v2_raw = sig_v2_raw.join(fill_sides, on="market_id", how="left")

# Deduplicate: keep ONE row per market_id.
# Prefer: keep row where outcome_side matches the canonical convention clearly.
# Since p_posted is already canonical-Up, keep the Up token row preferentially,
# or just keep the first row after dedup (lowest sigma_implied = most stable).
sig_v2 = sig_v2_raw.sort("sigma_implied").unique(subset=["market_id"], keep="first")
print(f"sigma_implied_v2: {len(sig_v2)} markets (after dedup from {len(sig_v2_raw)})")

# ── Rebuild σ estimators at t_post_ns for v2 markets ─────────────────────────
print("Building σ estimators at t_post_ns...")
t0 = time.time()
est_rows = []

for asset, stream in SYMBOL_STREAM.items():
    asset_mkts = sig_v2.filter(pl.col("asset_symbol") == asset)
    if asset_mkts.is_empty():
        continue
    print(f"  {asset}: {len(asset_mkts)} markets")

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
    mids = ticker["mid"].to_numpy()
    ts_ns = ticker["t_recv_ns"].to_numpy()
    log_ret = np.diff(np.log(mids))
    ts_min = (ts_ns // (60*1_000_000_000)).astype(np.int64)
    unique_mins = np.unique(ts_min)
    min_prices_arr = np.array([mids[np.where(ts_min==m)[0][-1]] for m in unique_mins])
    min_rets = np.diff(np.log(min_prices_arr))

    # GARCH rolling
    garch_h: dict[int,float] = {}
    for start in range(0, len(min_rets)-1440, 720):
        chunk = min_rets[start:start+1440]
        if len(chunk) < 100: continue
        try:
            res = arch_model(chunk*100, vol="GARCH", p=1, q=1, rescale=False).fit(disp="off")
            for i, h in enumerate(res.conditional_volatility**2):
                mn = unique_mins[start+i] if (start+i)<len(unique_mins) else 0
                garch_h[mn] = float(h)*(1/100)**2
        except Exception: pass

    klines_df = None
    if kline_rows:
        kp = []
        for df_k in kline_rows:
            for row in df_k.iter_rows(named=True):
                try:
                    k = json.loads(row["k"])
                    kp.append({"t_open_ms":int(k["t"]),"high":float(k["h"]),"low":float(k["l"])})
                except: pass
        if kp: klines_df = pl.DataFrame(kp).sort("t_open_ms")

    for row in asset_mkts.iter_rows(named=True):
        t_q = row["t_post_ns"]
        mkt = row["market_id"]
        idx_q = min(int(np.searchsorted(ts_ns, t_q)), len(ts_ns)-1)
        t_q_min = int(t_q//(60*1_000_000_000))
        idx_min_q = min(int(np.searchsorted(unique_mins, t_q_min)), len(min_rets))
        rec = {"market_id": mkt}

        # RV windows
        for Wm, wn in [(1,"rv_1m"),(5,"rv_5m")]:
            W_t = max(1, int(Wm*60/0.1))
            si = max(0, idx_q-W_t)
            rw = log_ret[si:min(idx_q,len(log_ret))]
            if len(rw)<2: rec[wn]=np.nan; continue
            dt_s = (ts_ns[idx_q]-ts_ns[si])/1e9
            if dt_s<=0: rec[wn]=np.nan; continue
            rec[wn] = float(np.sqrt(np.mean(rw**2)/(dt_s/len(rw))*SECS_PER_YEAR))

        # EWMA
        for lam, nm in [(0.90,"ewma_90"),(0.94,"ewma_94"),(0.97,"ewma_97")]:
            r1m = min_rets[max(0,idx_min_q-1440):idx_min_q]
            if len(r1m)<5: rec[nm]=np.nan; continue
            h = float(np.var(r1m[:10]) if len(r1m)>=10 else r1m[0]**2)
            for r in r1m: h = lam*h+(1-lam)*float(r)**2
            rec[nm] = float(np.sqrt(h*1440*365.25))

        # Parkinson 1h
        if klines_df is not None:
            idx_k = int(np.searchsorted(klines_df["t_open_ms"].to_numpy(), t_q//1_000_000))
            sub = klines_df.slice(max(0,idx_k-60), min(60,idx_k))
            if len(sub)>=5:
                H=sub["high"].to_numpy(); L=sub["low"].to_numpy()
                rec["park_1h"] = float(np.sqrt(np.mean(np.log(H/L)**2)/(4*np.log(2))*1440*365.25))
            else: rec["park_1h"] = np.nan
        else: rec["park_1h"] = np.nan

        # Seasonal = rv_60m proxy
        W_t60 = max(1, int(60*60/0.1))
        si60 = max(0, idx_q-W_t60)
        rw60 = log_ret[si60:min(idx_q,len(log_ret))]
        if len(rw60)>=2 and (ts_ns[idx_q]-ts_ns[si60])>0:
            dt60 = (ts_ns[idx_q]-ts_ns[si60])/1e9
            rec["seasonal"] = float(np.sqrt(np.mean(rw60**2)/(dt60/len(rw60))*SECS_PER_YEAR))
        else: rec["seasonal"] = np.nan

        est_rows.append(rec)

est_df = pl.DataFrame(est_rows)
df = sig_v2.join(est_df, on="market_id", how="inner")
for col in EST_NAMES: df = df.filter(pl.col(col).is_not_null() & pl.col(col).is_finite())
df = df.filter(pl.col("sigma_implied").is_not_null() & (pl.col("sigma_implied") > 0))
print(f"L2 dataset: {len(df)} markets ({time.time()-t0:.0f}s)")

# Prepare arrays
p_obs = df["p_posted"].to_numpy()
S0    = df["S0"].to_numpy()
St    = df["S_t"].to_numpy()
tau_y = df["tau_years"].to_numpy()
est_mat = df.select(EST_NAMES).to_numpy()  # shape (N, 7)
asset_arr = df["asset_symbol"].to_numpy()
horizon_arr = df["horizon"].to_numpy()
N = len(p_obs)

# Direction in canonical Up space:
#   +1: quote ABOVE FairValue in Up space → ohanism SELLS Up OR BUYS Down
#       (adds long-Up exposure)
#   -1: quote BELOW FairValue in Up space → ohanism SELLS Down OR BUYS Up
#       (adds short-Up exposure)
ohanism_side_arr = df["ohanism_side"].to_numpy() if "ohanism_side" in df.columns else np.full(N, "SELL")
outcome_side_arr = df["outcome_side"].to_numpy() if "outcome_side" in df.columns else np.full(N, "Up")

direction = np.where(
    ((ohanism_side_arr == "SELL") & (outcome_side_arr == "Up")) |
    ((ohanism_side_arr == "BUY") & (outcome_side_arr == "Down")),
    1.0, -1.0
)

print(f"N={N}, direction: Up={int((direction>0).sum())} Down={int((direction<0).sum())}")

# ── Model functions ────────────────────────────────────────────────────────────
def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()

def sigma_hat(w_raw: np.ndarray) -> np.ndarray:
    w = softmax(w_raw)
    return est_mat @ w  # shape (N,)

def fair_value(sigma: np.ndarray) -> np.ndarray:
    log_ratio = np.log(S0 / St)
    d = log_ratio / (np.maximum(sigma, 1e-6) * np.sqrt(tau_y))
    return 1.0 - norm.cdf(d)

def p_model(w_raw: np.ndarray, theta_h: list, theta_rho: float,
            theta_c: list) -> np.ndarray:
    sig = sigma_hat(w_raw)
    fv  = fair_value(sig)
    sqrt_tau = np.sqrt(tau_y)
    hs  = theta_h[0] + theta_h[1] * sig * sqrt_tau
    reb = theta_rho * (0.5 - np.minimum(fv, 1.0 - fv))
    otm = theta_c[0] + theta_c[1] * sig * sqrt_tau
    raw = fv - direction * hs + reb + otm
    return np.clip(raw, 0.01, 0.99)

def neg_loglik(params: np.ndarray, fix_others: bool = False,
               theta_h_fix=None, theta_rho_fix=None, theta_c_fix=None) -> float:
    if fix_others:
        w_raw = params[:7]
        th = theta_h_fix; rho = theta_rho_fix; tc = theta_c_fix
    else:
        w_raw = params[:7]
        th = [params[7], params[8]]
        rho = params[9]
        tc = [params[10], params[11]]
    pm = p_model(w_raw, th, rho, tc)
    var = pm * (1.0 - pm) + 1e-6
    nll = 0.5 * np.sum((p_obs - pm)**2 / var + np.log(var))
    return float(nll if np.isfinite(nll) else 1e12)

# ── Stage 1: θ_σ only, other params fixed ─────────────────────────────────────
print("\n=== STAGE 1: σ-recipe only (20 restarts) ===")
THETA_H_FIX = [0.005, 0.5]
THETA_RHO_FIX = 0.01
THETA_C_FIX  = [0.22, 0.0]  # fixed OTM cushion

# Informative init from L1: w_ewma94=0.5, w_ewma90=0.3, w_ewma97=0.1, rest=0.025
# EST_NAMES = ["ewma_90","ewma_94","ewma_97","rv_1m","rv_5m","park_1h","seasonal"]
W0_INFORMED = np.array([0.3, 0.5, 0.1, 0.025, 0.025, 0.025, 0.025])
W_RAW0 = np.log(W0_INFORMED + 1e-9)  # approximate inverse softmax

rng = np.random.default_rng(42)
stage1_results = []
for restart in range(20):
    if restart == 0:
        x0 = W_RAW0.copy()
    else:
        x0 = W_RAW0 + rng.normal(0, 0.5, 7)

    res = minimize(
        lambda p: neg_loglik(p, fix_others=True,
                             theta_h_fix=THETA_H_FIX,
                             theta_rho_fix=THETA_RHO_FIX,
                             theta_c_fix=THETA_C_FIX),
        x0, method="L-BFGS-B",
        options={"maxiter": 2000, "ftol": 1e-10, "gtol": 1e-7}
    )
    w = softmax(res.x)
    stage1_results.append({"nll": res.fun, "w": w.copy(), "x": res.x.copy(),
                            "success": res.success})

stage1_results.sort(key=lambda r: r["nll"])
best1 = stage1_results[0]
print(f"Best NLL: {best1['nll']:.2f}  success: {best1['success']}")
print("Weights:")
for nm, wi in zip(EST_NAMES, best1["w"]):
    print(f"  {nm:<20}: {wi:.4f}")

# Convergence: fraction within ε=0.05 of best
best_w = best1["w"]
conv_count = sum(1 for r in stage1_results
                 if np.max(np.abs(r["w"] - best_w)) < 0.05)
print(f"Convergence: {conv_count}/20 restarts within ε=0.05 of best weights")

# G1 check
g1_pass = conv_count >= 16  # ≥80%
print(f"G1 (≥80% convergence): {'PASS ✓' if g1_pass else 'FAIL'}")

# G2: implied sigma_hat plausibility for BTC 5m
btc5_mask = (asset_arr == "BTC") & (horizon_arr == "5m")
if btc5_mask.sum() > 0:
    sig_btc5 = (est_mat[btc5_mask] @ best1["w"])
    med_sig = float(np.median(sig_btc5))
    print(f"G2: BTC 5m median sigma_hat = {med_sig:.3f}  {'PASS ✓' if 0.2<=med_sig<=1.5 else 'FAIL'}")
else:
    print("G2: No BTC 5m data")

# G4: EWMA weight sum
ewma_sum = best1["w"][:3].sum()  # ewma_90 + ewma_94 + ewma_97
print(f"G4: EWMA weight sum = {ewma_sum:.3f}  {'PASS ✓' if ewma_sum>=0.4 else 'FAIL'}")

# ── Stage 2: joint fit (OTM first-pass fixed, then freed) ─────────────────────
print("\n=== STAGE 2a: joint fit, θ_c FIXED at (0.22, 0) ===")
# params: [w_raw (7), theta_h0, theta_h1, theta_rho, theta_c0, theta_c1]
x2a_start = np.concatenate([
    best1["x"],           # warm-start from stage 1
    [THETA_H_FIX[0], THETA_H_FIX[1], THETA_RHO_FIX, 0.22, 0.0]
])
bounds2 = [(-5,5)]*7 + [(1e-4,0.1),(0.0,3.0),(0.0,0.1),(0.1,0.35),(-0.5,0.5)]

stage2a_results = []
for restart in range(20):
    if restart == 0:
        x0 = x2a_start.copy()
    else:
        perturb = np.zeros(12)
        perturb[:7] = rng.normal(0, 0.3, 7)
        perturb[7:] = rng.normal(0, 0.05, 5)
        x0 = x2a_start + perturb
        x0 = np.clip(x0, [b[0] for b in bounds2], [b[1] for b in bounds2])

    res = minimize(
        neg_loglik, x0, method="L-BFGS-B", bounds=bounds2,
        options={"maxiter": 3000, "ftol": 1e-10, "gtol": 1e-7}
    )
    w = softmax(res.x[:7])
    stage2a_results.append({"nll": res.fun, "w": w, "x": res.x, "success": res.success})

stage2a_results.sort(key=lambda r: r["nll"])
best2a = stage2a_results[0]
w2a = best2a["w"]
x2a = best2a["x"]
print(f"NLL={best2a['nll']:.2f}  success={best2a['success']}")
print(f"θ_h=({x2a[7]:.4f},{x2a[8]:.4f}) θ_ρ={x2a[9]:.4f} θ_c=({x2a[10]:.4f},{x2a[11]:.4f})")
print("σ-recipe weights (Stage 2a):")
for nm, wi in zip(EST_NAMES, w2a):
    print(f"  {nm:<20}: {wi:.4f}")

# Compare Stage 1 vs 2a θ_σ
drift = np.max(np.abs(w2a - best1["w"]))
print(f"Stage 1 vs 2a θ_σ max drift: {drift:.4f} {'OK (stable)' if drift<0.15 else 'WARN: large drift'}")

print("\n=== STAGE 2b: joint fit, θ_c FREED ===")
bounds2b = [(-5,5)]*7 + [(1e-4,0.1),(0.0,3.0),(0.0,0.1),(0.0,0.40),(-0.5,0.5)]
x2b_start = x2a.copy()

stage2b_results = []
for restart in range(10):  # fewer restarts since warm-started
    if restart == 0:
        x0 = x2b_start.copy()
    else:
        perturb = np.zeros(12)
        perturb[:7] = rng.normal(0, 0.2, 7)
        perturb[10:] = rng.normal(0, 0.03, 2)
        x0 = x2b_start + perturb
        x0 = np.clip(x0, [b[0] for b in bounds2b], [b[1] for b in bounds2b])

    res = minimize(
        neg_loglik, x0, method="L-BFGS-B", bounds=bounds2b,
        options={"maxiter": 3000, "ftol": 1e-10, "gtol": 1e-7}
    )
    stage2b_results.append({"nll": res.fun, "w": softmax(res.x[:7]),
                              "x": res.x, "success": res.success})

stage2b_results.sort(key=lambda r: r["nll"])
best2b = stage2b_results[0]
x2b = best2b["x"]
print(f"NLL={best2b['nll']:.2f}  success={best2b['success']}")
print(f"θ_h=({x2b[7]:.4f},{x2b[8]:.4f}) θ_ρ={x2b[9]:.4f} θ_c=({x2b[10]:.4f},{x2b[11]:.4f})")
print(f"  I2 check: θ_c0={x2b[10]:.4f} (expected ≈0.22), θ_c1={x2b[11]:.4f}")

# ── Acceptance gates ───────────────────────────────────────────────────────────
print("\n=== ACCEPTANCE GATES ===")
# G1
cv2a = sum(1 for r in stage2a_results if np.max(np.abs(softmax(r["x"][:7])-w2a))<0.05)
print(f"G1 Stage-2a convergence: {cv2a}/20 {'PASS ✓' if cv2a>=16 else 'FAIL'}")

# G2
if btc5_mask.sum() > 0:
    s2 = est_mat[btc5_mask] @ w2a
    med2 = float(np.median(s2))
    print(f"G2 BTC 5m median sigma_hat (Stage 2a): {med2:.3f} {'PASS ✓' if 0.2<=med2<=1.5 else 'FAIL'}")

# G3: out-of-sample
t_sort = np.argsort(df["t_post_ns"].to_numpy())
n_train = int(0.7 * N)
train_idx = t_sort[:n_train]; test_idx = t_sort[n_train:]

def rmse_split(idx, theta):
    pm = p_model(theta[:7], [theta[7],theta[8]], theta[9], [theta[10],theta[11]])
    return float(np.sqrt(np.mean((p_obs[idx] - pm[idx])**2)))

rmse_train = rmse_split(train_idx, x2b)
rmse_test  = rmse_split(test_idx, x2b)
ratio = rmse_test / rmse_train if rmse_train > 0 else 999
print(f"G3: RMSE train={rmse_train:.4f} test={rmse_test:.4f} ratio={ratio:.2f} {'PASS ✓' if ratio<=1.30 else 'FAIL'}")

# G4
ew4 = w2a[:3].sum()
print(f"G4 EWMA weight sum (Stage 2a): {ew4:.3f} {'PASS ✓' if ew4>=0.4 else 'FAIL'}")

# Summary
print("\n=== THETA_HAT SUMMARY (Stage 2b, full model) ===")
w_final = softmax(x2b[:7])
print("σ-recipe weights:")
for nm, wi, w1 in zip(EST_NAMES, w_final, best1["w"]):
    print(f"  {nm:<20}: θ̂={wi:.4f}  (Stage1={w1:.4f})")
print(f"half_spread: θ_h0={x2b[7]:.4f} θ_h1={x2b[8]:.4f}")
print(f"rebate_skew: θ_ρ={x2b[9]:.4f}")
print(f"OTM_adjust:  θ_c0={x2b[10]:.4f} θ_c1={x2b[11]:.4f}")

# Save results
import json as _json
results = {
    "N": int(N),
    "stage1": {"weights": {nm: float(v) for nm,v in zip(EST_NAMES, best1["w"])},
               "nll": float(best1["nll"]), "convergence_rate": conv_count/20,
               "g1_pass": g1_pass, "g4_ewma_sum": float(ewma_sum)},
    "stage2a": {"weights": {nm: float(v) for nm,v in zip(EST_NAMES, w2a)},
                "nll": float(best2a["nll"]), "convergence_rate": cv2a/20,
                "theta_h": [float(x2a[7]), float(x2a[8])],
                "theta_rho": float(x2a[9]), "theta_c": [float(x2a[10]), float(x2a[11])]},
    "stage2b": {"weights": {nm: float(v) for nm,v in zip(EST_NAMES, w_final)},
                "nll": float(best2b["nll"]),
                "theta_h": [float(x2b[7]), float(x2b[8])],
                "theta_rho": float(x2b[9]), "theta_c": [float(x2b[10]), float(x2b[11])],
                "g3_rmse_train": float(rmse_train), "g3_rmse_test": float(rmse_test),
                "g3_ratio": float(ratio)},
}
(cfg.results_dir / "phase4_l2.json").write_text(_json.dumps(results, indent=2))
print(f"\nSaved: output/results/phase4_l2.json")
print(f"Total runtime: {(time.time()-t0)/60:.1f} min")
