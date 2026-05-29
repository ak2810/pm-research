"""Phase 4 Steps 4.2b + 4.3b: σ estimators at t_post_ns + L1 cascade on σ_implied_v2.

Uses sigma_implied_v2.parquet (quote-post-time σ).
Rebuilds σ_estimators at t_post_ns (not t_first_fill_ns).
Runs M1-M5 regression cascade with HAC SEs, gate R² >= 0.4.
"""
import sys
import json
import time
import warnings

sys.path.insert(0, "src")

import numpy as np
import polars as pl
import statsmodels.api as sm
from arch import arch_model  # type: ignore[import-untyped]
from statsmodels.stats.outliers_influence import variance_inflation_factor  # type: ignore[import-untyped]

warnings.filterwarnings("ignore")

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()

SECS_PER_YEAR = 365.25 * 24 * 3600
SYMBOL_STREAM = {
    "BTC": "btcusdt", "ETH": "ethusdt",
    "SOL": "solusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt",
}
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]

t0 = time.time()

sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
print(f"σ_implied_v2: {len(sig_v2)} markets")

# ── Build σ_estimators at t_post_ns ──────────────────────────────────────────
print("Building σ estimators at t_post_ns...")
all_records = []

for asset, stream in SYMBOL_STREAM.items():
    asset_mkts = sig_v2.filter(pl.col("asset_symbol") == asset)
    if asset_mkts.is_empty():
        continue
    print(f"  {asset}: {len(asset_mkts)} markets")

    # Load bookTicker
    ticker_rows = []
    kline_rows = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e","s","b","a","t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).with_columns(
                ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
            ).select(["t_recv_ns","mid"]).collect()
            if len(df): ticker_rows.append(df)
        except FileNotFoundError: pass

        try:
            lf_k = scan_feed("binance", date, columns=["e","s","k","t_recv_ns"])
            df_k = lf_k.filter(
                (pl.col("e") == "kline") & (pl.col("s").str.to_lowercase() == stream)
                & pl.col("k").is_not_null()
            ).collect()
            if len(df_k): kline_rows.append(df_k)
        except FileNotFoundError: pass

    if not ticker_rows:
        continue

    ticker = pl.concat(ticker_rows).sort("t_recv_ns")
    mids = ticker["mid"].to_numpy()
    ts_ns = ticker["t_recv_ns"].to_numpy()
    log_ret = np.diff(np.log(mids))

    # 1-minute return series for GARCH/EWMA
    ts_min = (ts_ns // (60 * 1_000_000_000)).astype(np.int64)
    unique_mins = np.unique(ts_min)
    min_prices = [mids[np.where(ts_min == m)[0][-1]] for m in unique_mins]
    min_prices_arr = np.array(min_prices)
    min_rets = np.diff(np.log(min_prices_arr))

    # Fit GARCH rolling
    garch_h: dict[int, float] = {}
    window = 1440
    for start in range(0, len(min_rets) - window, window // 2):
        chunk = min_rets[start: start + window]
        if len(chunk) < 100: continue
        try:
            am = arch_model(chunk * 100, vol="GARCH", p=1, q=1, rescale=False)
            res = am.fit(disp="off")
            h_series = res.conditional_volatility ** 2
            for i, h in enumerate(h_series):
                mn = unique_mins[start + i] if (start + i) < len(unique_mins) else 0
                garch_h[mn] = float(h) * (1/100)**2
        except Exception: pass

    # Parse klines
    klines_df = None
    if kline_rows:
        kp = []
        for df_k in kline_rows:
            for row in df_k.iter_rows(named=True):
                try:
                    k = json.loads(row["k"])
                    kp.append({"t_open_ms": int(k["t"]), "high": float(k["h"]),
                               "low": float(k["l"]), "open": float(k["o"]), "close": float(k["c"])})
                except: pass
        if kp:
            klines_df = pl.DataFrame(kp).sort("t_open_ms")

    for row in asset_mkts.iter_rows(named=True):
        t_q = row["t_post_ns"]  # USE t_post_ns, not t_first_fill_ns
        mkt = row["market_id"]

        idx_q = int(np.searchsorted(ts_ns, t_q))
        if idx_q >= len(ts_ns): idx_q = len(ts_ns) - 1

        rec: dict = {"market_id": mkt, "asset_symbol": asset, "horizon": row.get("horizon","")}

        # Realized vol windows
        for W_min in [1, 5, 15, 30, 60, 240, 1440]:
            W_ticks = max(1, int(W_min * 60 / 0.1))
            si = max(0, idx_q - W_ticks)
            ret_w = log_ret[si: min(idx_q, len(log_ret))]
            if len(ret_w) < 2:
                rec[f"rv_{W_min}m"] = np.nan; continue
            dt_s = (ts_ns[idx_q] - ts_ns[si]) / 1e9
            if dt_s <= 0:
                rec[f"rv_{W_min}m"] = np.nan; continue
            mean_dt = dt_s / len(ret_w)
            rec[f"rv_{W_min}m"] = float(np.sqrt(np.mean(ret_w**2) / mean_dt * SECS_PER_YEAR))

        # EWMA
        t_q_min = int(t_q // (60 * 1_000_000_000))
        idx_min_q = int(np.searchsorted(unique_mins, t_q_min))
        if idx_min_q > len(min_rets): idx_min_q = len(min_rets)

        for lam in [0.90, 0.94, 0.97, 0.99]:
            ret_1m = min_rets[max(0, idx_min_q - 1440): idx_min_q]
            if len(ret_1m) < 5:
                rec[f"ewma_{int(lam*100)}"] = np.nan; continue
            h = float(np.var(ret_1m[:10]) if len(ret_1m) >= 10 else ret_1m[0]**2)
            for r in ret_1m:
                h = lam * h + (1-lam) * float(r)**2
            rec[f"ewma_{int(lam*100)}"] = float(np.sqrt(h * 1440 * 365.25))

        # GARCH
        gv = garch_h.get(t_q_min)
        rec["garch"] = float(np.sqrt(gv * 1440 * 365.25)) if gv else np.nan

        # Parkinson/GK from klines
        if klines_df is not None:
            t_q_ms = t_q // 1_000_000
            k_ts = klines_df["t_open_ms"].to_numpy()
            idx_k = int(np.searchsorted(k_ts, t_q_ms))
            for label, n_c in [("park_30m",30),("park_1h",60),("gk_1h",60)]:
                sub = klines_df.slice(max(0, idx_k-n_c), min(idx_k-max(0,idx_k-n_c), n_c))
                if len(sub) < 5:
                    rec[label] = np.nan; continue
                H = sub["high"].to_numpy(); L = sub["low"].to_numpy()
                O = sub["open"].to_numpy(); C = sub["close"].to_numpy()
                if label.startswith("park"):
                    rec[label] = float(np.sqrt(np.mean(np.log(H/L)**2)/(4*np.log(2)) * 1440*365.25))
                else:
                    gk = np.mean(0.5*np.log(H/L)**2 - (2*np.log(2)-1)*np.log(C/O)**2)
                    rec[label] = float(np.sqrt(max(0,gk) * 1440*365.25))
        else:
            for label in ["park_30m","park_1h","gk_1h"]:
                rec[label] = np.nan

        # Intraday seasonal (hour-of-day σ_rv_60m placeholder — filled after)
        rec["intraday_seasonal"] = rec.get("rv_60m", np.nan)

        all_records.append(rec)

    print(f"    {asset}: {len([r for r in all_records if r['asset_symbol']==asset])} records")

df_est = pl.DataFrame(all_records)
print(f"Estimators built: {len(df_est)} rows")

# ── Merge with σ_implied_v2 ───────────────────────────────────────────────────
EST_COLS = ["rv_1m","rv_5m","rv_15m","rv_30m","rv_60m","rv_240m","rv_1440m",
            "ewma_90","ewma_94","ewma_97","ewma_99","garch",
            "park_30m","park_1h","gk_1h","intraday_seasonal"]

df = sig_v2.join(df_est.drop(["asset_symbol","horizon"]), on="market_id", how="inner")
df = df.drop_nulls(subset=["sigma_implied"] + EST_COLS)
print(f"Joint dataset: {len(df)} markets")

y = df["sigma_implied"].to_numpy()
T = len(y)
hac_lag = max(1, round(4 * (T/100)**(2/9)))
print(f"n={T}, HAC lag={hac_lag}")

# ── M1 ────────────────────────────────────────────────────────────────────────
print("\n=== M1: UNIVARIATE ===")
m1_results = []
for col in EST_COLS:
    X_raw = df[col].to_numpy().astype(float)
    valid = np.isfinite(X_raw) & np.isfinite(y)
    if valid.sum() < 20: continue
    X = X_raw[valid]; yv = y[valid]
    T_s = valid.sum(); hl = max(1, round(4*(T_s/100)**(2/9)))
    res = sm.OLS(yv, sm.add_constant(X)).fit(cov_type="HAC", cov_kwds={"maxlags": hl})
    b, se, tv, pv = res.params[1], res.bse[1], res.tvalues[1], res.pvalues[1]
    r2 = res.rsquared
    m1_results.append({"estimator": col, "beta": b, "SE": se, "t": tv, "p": pv, "R2": r2,
                        "RMSE": float(np.sqrt(np.mean(res.resid**2)))})
    print(f"  {col:<20}: R²={r2:.3f} β={b:.3f} SE={se:.3f} t={tv:.2f} p={pv:.3f}")

m1_sorted = sorted(m1_results, key=lambda x: x["R2"], reverse=True)
print(f"\nTop-5: " + ", ".join(f"{r['estimator']}:{r['R2']:.3f}" for r in m1_sorted[:5]))

# Diverse cross-family top-3
def best_from(subset, all_res):
    rel = [r for r in all_res if r["estimator"] in subset]
    return max(rel, key=lambda x: x["R2"])["estimator"] if rel else subset[0]

top3 = [
    best_from(["ewma_90","ewma_94","ewma_97","ewma_99","garch"], m1_results),
    best_from(["rv_1m","rv_5m","rv_15m","rv_30m","rv_60m","rv_240m","rv_1440m"], m1_results),
    best_from(["park_30m","park_1h","gk_1h","intraday_seasonal"], m1_results),
]
print(f"Diverse top-3: {top3}")

# ── M2-M5 ─────────────────────────────────────────────────────────────────────
X_top3_raw = df.select(top3).to_numpy().astype(float)
finite_mask = np.all(np.isfinite(X_top3_raw), axis=1) & np.isfinite(y)
y_f = y[finite_mask]; X_t3 = X_top3_raw[finite_mask]
df_f = df.filter(pl.Series(finite_mask))
Tf = int(finite_mask.sum()); hf = max(1, round(4*(Tf/100)**(2/9)))
print(f"Joint models: n={Tf}, HAC lag={hf}")

def fit_hac(y_arr, X_arr, hac_lags):
    return sm.OLS(y_arr, X_arr).fit(cov_type="HAC", cov_kwds={"maxlags": hac_lags})

# M2
res2 = fit_hac(y_f, sm.add_constant(X_t3), hf)
vifs = [variance_inflation_factor(X_t3, i) for i in range(3)]
print(f"\nM2 R²={res2.rsquared:.3f} adj={res2.rsquared_adj:.3f} VIF:{vifs[0]:.1f}/{vifs[1]:.1f}/{vifs[2]:.1f}")

# M3: +asset FEs
assets = sorted(df_f["asset_symbol"].unique().to_list())
adm = np.zeros((Tf, len(assets)-1))
for i, a in enumerate(assets[1:]): adm[:,i] = (df_f["asset_symbol"]==a).to_numpy().astype(float)
res3 = fit_hac(y_f, np.column_stack([np.ones(Tf), X_t3, adm]), hf)
print(f"M3 R²={res3.rsquared:.3f} adj={res3.rsquared_adj:.3f}")
for nm, b, se, tv, pv in zip(["const"]+top3+[f"FE_{a}" for a in assets[1:]],
                               res3.params, res3.bse, res3.tvalues, res3.pvalues):
    print(f"  {nm:<22}: β={b:.3f} SE={se:.3f} t={tv:.2f} p={pv:.3f}")
stable3 = all(np.sign(res3.params[i+1]) == np.sign(res2.params[i+1]) for i in range(3))
print(f"  Signs stable M2→M3: {'✓' if stable3 else 'FLIPPED'}")

# M4: +asset×horizon
ahs = sorted(df_f.select(["asset_symbol","horizon"]).unique().to_numpy().tolist())
ah_others = ahs[1:]
ahdm = np.zeros((Tf, len(ah_others)))
for j, (a,h) in enumerate(ah_others):
    ahdm[:,j] = ((df_f["asset_symbol"]==a) & (df_f["horizon"]==h)).to_numpy().astype(float)
res4 = fit_hac(y_f, np.column_stack([np.ones(Tf), X_t3, ahdm]), hf)
print(f"M4 R²={res4.rsquared:.3f} adj={res4.rsquared_adj:.3f}")

# M5: +hour FEs
df_f2 = df_f.with_columns((pl.col("t_post_ns") // 3_600_000_000_000 % 24).alias("hour_utc"))
hours = sorted(df_f2["hour_utc"].unique().to_list())
hdm = np.zeros((Tf, len(hours)-1))
for k, h in enumerate(hours[1:]): hdm[:,k] = (df_f2["hour_utc"]==h).to_numpy().astype(float)
res5 = fit_hac(y_f, np.column_stack([np.ones(Tf), X_t3, ahdm, hdm]), hf)
print(f"M5 R²={res5.rsquared:.3f} adj={res5.rsquared_adj:.3f}")
print(f"  top-3 coefs: " + " | ".join(
    f"{top3[i]}: β={res5.params[i+1]:.3f} t={res5.tvalues[i+1]:.2f}" for i in range(3)))

# Gate
print("\n=== GATE (R² ≥ 0.4, stable signs) ===")
m1_signs = {c: np.sign(next(r["beta"] for r in m1_results if r["estimator"]==c)) for c in top3}
gate_model = None
for nm, res in [("M2",res2),("M3",res3),("M4",res4),("M5",res5)]:
    r2 = res.rsquared
    stable = all(np.sign(res.params[i+1]) == m1_signs[top3[i]] for i in range(3))
    passed = r2 >= 0.4 and stable
    print(f"  {nm}: R²={r2:.3f} {'PASS ✓' if passed else 'FAIL'} stable={stable}")
    if passed and gate_model is None:
        gate_model = (nm, res)

if gate_model:
    print(f"\nGATE PASSES at {gate_model[0]}. σ recipe: {top3}")
    print("→ CASE A: Proceed to Step 4.4 per-asset residuals and Step 4.5 L2.")
else:
    best_r2 = max(res.rsquared for _, res in [("M2",res2),("M3",res3),("M4",res4),("M5",res5)])
    if best_r2 >= 0.4:
        print(f"\nBest R²={best_r2:.3f} ≥ 0.4 but signs unstable — investigate M2 collinearity.")
    else:
        print(f"\nGATE STILL FAILS. Best R²={best_r2:.3f} < 0.4")
        print("→ CASE B: Log BLOCKER-004.")

print(f"\nTotal runtime: {(time.time()-t0)/60:.1f} min")
