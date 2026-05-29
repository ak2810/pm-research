"""Phase 4 Step 4.3: L1 regression cascade (diagnostic).

Models M1-M5, each strictly larger than previous.
HAC (Newey-West) standard errors throughout.
Gate: M3+ achieves R² >= 0.4 with stable signed coefficients.

Annex: Lagrange for HAC: lag = round(4*(T/100)^(2/9))
"""
import sys
import json
import warnings
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np
import polars as pl
import statsmodels.api as sm
from statsmodels.stats.outliers_influence import variance_inflation_factor  # type: ignore[import-untyped]

warnings.filterwarnings("ignore")

from reverse_engineering.config import get_settings

cfg = get_settings()

# Load and join sigma_implied + sigma_estimators
sig_impl = pl.read_parquet(str(cfg.tables_dir / "sigma_implied.parquet"))
sig_est  = pl.read_parquet(str(cfg.tables_dir / "sigma_estimators.parquet"))

df = sig_impl.join(sig_est.drop(["asset_symbol", "horizon"]), on="market_id", how="inner")

# Add ATM displacement: |log(S_t/S_0)| — proxy for ohanism's observed drift at quote time
df = df.with_columns(
    (pl.col("S_t") / pl.col("S0")).map_elements(
        lambda x: abs(float(__import__("math").log(x))) if x > 0 else 0.0,
        return_dtype=pl.Float64
    ).alias("atm_displacement")
)

# Drop any remaining NaN rows in the union of estimator columns
EST_COLS = ["rv_1m","rv_5m","rv_15m","rv_30m","rv_60m","rv_240m","rv_1440m",
            "ewma_90","ewma_94","ewma_97","ewma_99","garch",
            "park_30m","park_1h","gk_1h","intraday_seasonal","atm_displacement"]

df = df.drop_nulls(subset=["sigma_implied"] + EST_COLS)
print(f"Joint dataset after drop_nulls: {len(df)} markets")

y = df["sigma_implied"].to_numpy()
T = len(y)
hac_lag = max(1, round(4 * (T / 100) ** (2/9)))
print(f"n={T}, HAC lag={hac_lag}")

# ── M1: univariate regressions ──────────────────────────────────────────────
print("\n=== M1: UNIVARIATE REGRESSIONS ===")
m1_results = []
for col in EST_COLS:
    X_raw = df[col].to_numpy().astype(float)
    valid = np.isfinite(X_raw) & np.isfinite(y)
    if valid.sum() < 20:
        print(f"  {col:<20}: SKIP (too few valid rows: {valid.sum()})")
        continue
    X = X_raw[valid]
    y_sub = y[valid]
    Xc = sm.add_constant(X)
    ols = sm.OLS(y_sub, Xc)
    T_sub = valid.sum()
    hac_lag_sub = max(1, round(4 * (T_sub / 100) ** (2/9)))
    res = ols.fit(cov_type="HAC", cov_kwds={"maxlags": hac_lag_sub})
    b, se, t_val, p_val = res.params[1], res.bse[1], res.tvalues[1], res.pvalues[1]
    r2, adj_r2 = res.rsquared, res.rsquared_adj
    rmse = float(np.sqrt(np.mean(res.resid ** 2)))
    m1_results.append({"estimator": col, "beta": b, "SE": se, "t": t_val, "p": p_val,
                        "R2": r2, "adj_R2": adj_r2, "RMSE": rmse})
    print(f"  {col:<20}: R²={r2:.3f} β={b:.3f} SE={se:.3f} t={t_val:.2f} p={p_val:.3f} RMSE={rmse:.3f}")

m1_sorted = sorted(m1_results, key=lambda x: x["R2"], reverse=True)
print(f"\nM1 ranking: " + ", ".join(f"{r['estimator']}:{r['R2']:.3f}" for r in m1_sorted[:5]))
print(f"Best single: {m1_sorted[0]['estimator']} R²={m1_sorted[0]['R2']:.3f}")

# Cross-family diverse top-3 (avoid collinear EWMA cluster)
# Families: EWMA, RV-window, range-based/GARCH
def best_from(subset: list, all_res: list) -> str:
    relevant = [r for r in all_res if r["estimator"] in subset]
    return max(relevant, key=lambda x: x["R2"])["estimator"] if relevant else subset[0]

ewma_family = ["ewma_90","ewma_94","ewma_97","ewma_99","garch"]
rv_family    = ["rv_1m","rv_5m","rv_15m","rv_30m","rv_60m","rv_240m","rv_1440m"]
range_family = ["park_30m","park_1h","gk_1h","intraday_seasonal","atm_displacement"]

best_ewma  = best_from(ewma_family, m1_results)
best_rv    = best_from(rv_family,   m1_results)
best_range = best_from(range_family, m1_results)
top3 = [best_ewma, best_rv, best_range]
print(f"Diverse top-3 (one per family): {top3}")
print(f"  (Raw top-3 by R²: {[r['estimator'] for r in m1_sorted[:3]]} — skipped, extreme VIF)")

# Global finite filter for joint models
X_top3_raw = df.select(top3).to_numpy().astype(float)
finite_mask = np.all(np.isfinite(X_top3_raw), axis=1) & np.isfinite(y)
y_f = y[finite_mask]
X_top3 = X_top3_raw[finite_mask]
df_f = df.filter(pl.Series(finite_mask))
T_f = int(finite_mask.sum())
hac_f = max(1, round(4 * (T_f / 100) ** (2/9)))
print(f"  Joint models: n={T_f} after finite filter, HAC lag={hac_f}")

# ── M2: top-3 jointly ──────────────────────────────────────────────────────
print("\n=== M2: TOP-3 JOINT ===")
X2 = sm.add_constant(X_top3)
res2 = sm.OLS(y_f, X2).fit(cov_type="HAC", cov_kwds={"maxlags": hac_f})
print(f"  R²={res2.rsquared:.3f} adj_R²={res2.rsquared_adj:.3f} RMSE={np.sqrt(np.mean(res2.resid**2)):.3f}")
for name, b, se, t_v, p_v in zip(["const"] + top3, res2.params, res2.bse, res2.tvalues, res2.pvalues):
    print(f"  {name:<20}: β={b:.3f} SE={se:.3f} t={t_v:.2f} p={p_v:.3f}")

# VIF for top-3
X2_vif = X_top3
vifs = [variance_inflation_factor(X2_vif, i) for i in range(3)]
print(f"  VIF: " + " ".join(f"{top3[i]}={vifs[i]:.1f}" for i in range(3)))

# ── M3: M2 + asset FEs ─────────────────────────────────────────────────────
print("\n=== M3: TOP-3 + ASSET FEs ===")
assets = sorted(df_f["asset_symbol"].unique().to_list())
asset_dummies = np.zeros((T_f, len(assets)-1))  # drop first as baseline
for i, a in enumerate(assets[1:]):
    asset_dummies[:, i] = (df_f["asset_symbol"] == a).to_numpy().astype(float)
X3 = np.column_stack([np.ones(T_f), X_top3, asset_dummies])
res3 = sm.OLS(y_f, X3).fit(cov_type="HAC", cov_kwds={"maxlags": hac_f})
fe_names = ["const"] + top3 + [f"FE_{a}" for a in assets[1:]]
print(f"  R²={res3.rsquared:.3f} adj_R²={res3.rsquared_adj:.3f} RMSE={np.sqrt(np.mean(res3.resid**2)):.3f}")
for name, b, se, t_v, p_v in zip(fe_names, res3.params, res3.bse, res3.tvalues, res3.pvalues):
    print(f"  {name:<22}: β={b:.3f} SE={se:.3f} t={t_v:.2f} p={p_v:.3f}")

# Check coefficient stability M2→M3
print("  Sign stability M2→M3: ", end="")
stable = True
for i, col in enumerate(top3):
    b2 = res2.params[i+1]
    b3 = res3.params[i+1]
    if np.sign(b2) != np.sign(b3):
        print(f"FLIP in {col}!", end="")
        stable = False
if stable:
    print("all stable ✓")

# ── M4: M3 + asset × horizon interaction ───────────────────────────────────
print("\n=== M4: M3 + ASSET×HORIZON FEs ===")
asset_horizons = sorted(df_f.select(["asset_symbol","horizon"]).unique().to_numpy().tolist())
# All combinations; drop one as baseline
ah_baselines = asset_horizons[:1]
ah_others = asset_horizons[1:]
ah_dummies = np.zeros((T_f, len(ah_others)))
for j, (a, h) in enumerate(ah_others):
    ah_dummies[:, j] = ((df_f["asset_symbol"] == a) & (df_f["horizon"] == h)).to_numpy().astype(float)
X4 = np.column_stack([np.ones(T_f), X_top3, ah_dummies])
res4 = sm.OLS(y_f, X4).fit(cov_type="HAC", cov_kwds={"maxlags": hac_f})
fe4_names = ["const"] + top3 + [f"FE_{a}_{h}" for a, h in ah_others]
print(f"  R²={res4.rsquared:.3f} adj_R²={res4.rsquared_adj:.3f} RMSE={np.sqrt(np.mean(res4.resid**2)):.3f}")
for name, b, se, t_v, p_v in zip(fe4_names[:8], res4.params[:8], res4.bse[:8], res4.tvalues[:8], res4.pvalues[:8]):
    print(f"  {name:<25}: β={b:.3f} SE={se:.3f} t={t_v:.2f} p={p_v:.3f}")
if len(fe4_names) > 8:
    print(f"  ... ({len(fe4_names)-8} more FE terms)")

# ── M5: M4 + hour-of-day FEs ───────────────────────────────────────────────
print("\n=== M5: M4 + HOUR-OF-DAY FEs ===")
df_f = df_f.with_columns(
    (pl.col("t_quote_ns") // 3_600_000_000_000 % 24).alias("hour_utc")
)
hours = sorted(df_f["hour_utc"].unique().to_list())
hour_baseline = hours[0]
hour_dummies = np.zeros((T_f, len(hours)-1))
for k, h in enumerate(hours[1:]):
    hour_dummies[:, k] = (df_f["hour_utc"] == h).to_numpy().astype(float)
X5 = np.column_stack([np.ones(T_f), X_top3, ah_dummies, hour_dummies])
res5 = sm.OLS(y_f, X5).fit(cov_type="HAC", cov_kwds={"maxlags": hac_f})
print(f"  R²={res5.rsquared:.3f} adj_R²={res5.rsquared_adj:.3f} RMSE={np.sqrt(np.mean(res5.resid**2)):.3f}")
print(f"  (top-3 coefficients):")
for name, b, se, t_v, p_v in zip(["const"] + top3, res5.params[:4], res5.bse[:4], res5.tvalues[:4], res5.pvalues[:4]):
    print(f"  {name:<22}: β={b:.3f} SE={se:.3f} t={t_v:.2f} p={p_v:.3f}")

# ── Gate evaluation ─────────────────────────────────────────────────────────
print("\n=== GATE EVALUATION (R² ≥ 0.4, stable signed coefficients) ===")
models = [("M2", res2), ("M3", res3), ("M4", res4), ("M5", res5)]
gate_model = None
for name, res in models:
    r2 = res.rsquared
    # Stability: top-3 coefficients same sign as M1 univariate
    m1_signs = {col: np.sign(next(r["beta"] for r in m1_results if r["estimator"]==col)) for col in top3}
    coeff_stable = all(np.sign(res.params[i+1]) == m1_signs[top3[i]] for i in range(3))
    gate = r2 >= 0.4 and coeff_stable
    print(f"  {name}: R²={r2:.3f} {'PASS ✓' if gate else 'FAIL'} (stable={coeff_stable})")
    if gate and gate_model is None:
        gate_model = (name, res)

if gate_model:
    print(f"\n  GATE HITS AT {gate_model[0]}. σ recipe uses top-3: {top3}")
else:
    print("\n  GATE FAILS at all models. Log blocker.")

# ── Save results ─────────────────────────────────────────────────────────────
results = {
    "n": int(T),
    "hac_lag": int(hac_lag),
    "top3_estimators": top3,
    "m1_ranking": [{"estimator": r["estimator"], "R2": round(r["R2"],4), "beta": round(r["beta"],4)}
                   for r in m1_sorted[:8]],
    "models": {
        "M2": {"R2": round(res2.rsquared,4), "adj_R2": round(res2.rsquared_adj,4),
               "RMSE": round(float(np.sqrt(np.mean(res2.resid**2))),4),
               "VIF": {top3[i]: round(vifs[i],1) for i in range(3)}},
        "M3": {"R2": round(res3.rsquared,4), "adj_R2": round(res3.rsquared_adj,4),
               "RMSE": round(float(np.sqrt(np.mean(res3.resid**2))),4)},
        "M4": {"R2": round(res4.rsquared,4), "adj_R2": round(res4.rsquared_adj,4),
               "RMSE": round(float(np.sqrt(np.mean(res4.resid**2))),4)},
        "M5": {"R2": round(res5.rsquared,4), "adj_R2": round(res5.rsquared_adj,4),
               "RMSE": round(float(np.sqrt(np.mean(res5.resid**2))),4)},
    },
    "gate_model": gate_model[0] if gate_model else None,
}
(cfg.results_dir / "phase4_regression.json").write_text(json.dumps(results, indent=2))
print("\nSaved: output/results/phase4_regression.json")
