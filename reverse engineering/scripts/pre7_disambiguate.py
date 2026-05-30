"""Pre-7 — Disambiguate Hypothesis A (per-market σ) vs Hypothesis B (EWMA σ + spread).

D1. Compute EWMA σ at t_post for each market in sigma_implied_v2.
D2. var(σ_implied - σ_EWMA) / var(σ_EWMA) per asset. Ratio<0.3 → B, >1 → A.
D3. Sub-window (6h) stability of σ-recipe correlation. CV<0.05 → B.
D4. OOS R² for σ_EWMA → σ_implied. R²>0.5 → B.
D5. p_posted vs FV_EWMA plot. Tight tracking → B.

Selection rule: compare σ_EWMA, OTM stats, hour-of-day for QUOTED vs DECLINED markets.

Decision: 3+ tests support B → paper twin uses EWMA σ + spread.
         3+ tests support A → paper twin needs to identify per-market σ rule.
         Mixed → build both.

Standing data-window rule S1-S5.
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, "src")

import numpy as np
import polars as pl
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import norm
from numpy.linalg import lstsq
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.gamma import _load_cached_cids

cfg = get_settings()
t0 = time.time()
np.random.seed(42)

# ── S1-S5 ─────────────────────────────────────────────────────────────────────
FEEDS = ["pm_clob","polygon","binance","pm_meta"]
WINDOW_START = ("2026-05-27",4)
feed_parts = {f: set((p.date,p.hour) for p in list_local_partitions(f)) for f in FEEDS}
common = None
for f in FEEDS:
    common = feed_parts[f] if common is None else common & feed_parts[f]
common = {p for p in common if p >= WINDOW_START}
common_sorted = sorted(common)
WINDOW_END = common_sorted[-1]
WINDOW_DATES = sorted(set(d for d,_ in common_sorted))
print(f"=== PRE-7: A vs B DISAMBIGUATION ===")
print(f"Window: {WINDOW_START} → {WINDOW_END} ({len(common_sorted)}h)")

# ── D1: Load sigma_implied_v2 + compute EWMA σ at t_post ─────────────────────
sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
print(f"\nD1: {len(sig_v2)} markets in sigma_implied_v2")

SYMBOL_STREAM = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT","DOGE":"DOGEUSDT"}
LAMBDA = 0.94  # Phase 4 Stage 1 result

# Load all Binance bookTicker and build per-symbol EWMA series
print("Loading Binance bookTicker...")
bticker_rows = []
for date in WINDOW_DATES:
    for pq in sorted(cfg.cache_dir.glob(f"feed=binance/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(pq), low_memory=True, hive_partitioning=False,
                             use_statistics=False)
        df = (lf.filter(pl.col("e").is_null() & pl.col("b").is_not_null())
              .select(["t_recv_ns","s","b","a"]).collect())
        if len(df): bticker_rows.append(df)

if not bticker_rows:
    raise RuntimeError("No Binance data found")

bticker = (pl.concat(bticker_rows)
           .with_columns([pl.col("b").cast(pl.Float64).alias("bid"),
                          pl.col("a").cast(pl.Float64).alias("ask")])
           .with_columns(((pl.col("bid")+pl.col("ask"))/2).alias("mid"))
           .sort("t_recv_ns"))
print(f"  {len(bticker):,} bookTicker ticks")

# Build per-symbol EWMA variance series using 1-MINUTE resampled returns.
# Tick-level EWMA from bookTicker captures microstructure noise (~1% annualized)
# not directional vol (~60% for BTC). Resample to 1m bars before EWMA.
print("  Computing EWMA σ from 1-min resampled returns per symbol...")
ewma_by_sym: dict[str, tuple[np.ndarray, np.ndarray]] = {}
SECONDS_PER_YEAR = 365.25 * 24 * 3600
BAR_S = 60  # 1-minute bar (in seconds)
for sym in set(SYMBOL_STREAM.values()):
    sub = bticker.filter(pl.col("s")==sym).sort("t_recv_ns")
    if len(sub) < 10: continue
    ts  = sub["t_recv_ns"].to_numpy()
    mid = sub["mid"].to_numpy()
    # Resample: snap each tick to the nearest 1-minute bar
    bar_idx = ts // (BAR_S * 10**9)  # integer bar label (minutes since epoch)
    # Get last mid price in each bar
    bar_labels, bar_last_idx = np.unique(bar_idx, return_index=True)
    # For each bar, take the LAST tick in that bar
    bar_last_idx_end = np.append(bar_last_idx[1:]-1, len(ts)-1)
    bar_ts  = ts[bar_last_idx_end]
    bar_mid = mid[bar_last_idx_end]
    # Log returns on 1-minute bars
    log_ret_1m = np.log(bar_mid[1:]/np.maximum(bar_mid[:-1],1e-9))
    # EWMA variance annualized: var is per-bar, annualize by bars/year
    bars_per_year = SECONDS_PER_YEAR / BAR_S
    ewma_var = np.zeros(len(log_ret_1m))
    ewma_var[0] = log_ret_1m[0]**2
    for i in range(1, len(log_ret_1m)):
        ewma_var[i] = LAMBDA*ewma_var[i-1] + (1-LAMBDA)*log_ret_1m[i]**2
    ewma_sigma = np.sqrt(ewma_var * bars_per_year)  # annualized σ
    ewma_by_sym[sym] = (bar_ts[1:], ewma_sigma)
    print(f"    {sym}: {len(ewma_sigma)} 1m bars, median σ={np.median(ewma_sigma):.3f}")

def get_ewma_sigma_at(sym: str, t_ns: int) -> float | None:
    if sym not in ewma_by_sym: return None
    ts, sigs = ewma_by_sym[sym]
    idx = np.searchsorted(ts, t_ns, side="right")-1
    if idx < 0: return None
    return float(sigs[idx])

# Enrich sigma_implied_v2 with EWMA σ at t_post
print("  Enriching sigma_implied_v2 with EWMA σ...")
rows_aug = []
for row in sig_v2.iter_rows(named=True):
    asset = str(row["asset_symbol"])
    sym   = SYMBOL_STREAM.get(asset,"btcusdt")
    t_post = int(row["t_post_ns"])
    ewma_s = get_ewma_sigma_at(sym, t_post)
    sigma_i = float(row["sigma_implied"])
    S0 = float(row["S0"]); St = float(row["S_t"]); tau = float(row["tau_years"])
    pp = float(row["p_posted"])
    log_r = np.log(max(S0,1e-9)/max(St,1e-9))
    # FV under σ_implied (circular) and FV under σ_EWMA (independent)
    fv_imp   = float(1-norm.cdf(log_r/max(sigma_i*tau**0.5,1e-8))) if sigma_i>0 else 0.5
    fv_ewma  = float(1-norm.cdf(log_r/max(ewma_s*tau**0.5,1e-8))) if ewma_s else 0.5
    hour_utc = (t_post//1_000_000_000//3600)%24
    rows_aug.append({**row, "sigma_ewma":ewma_s, "fv_ewma":fv_ewma, "fv_implied":fv_imp,
                     "hour_utc": hour_utc})

df = pl.DataFrame(rows_aug)
n_ewma = df.filter(pl.col("sigma_ewma").is_not_null()).height
print(f"  {n_ewma}/{len(df)} markets with EWMA σ")

# ── D2: Variance ratio test ───────────────────────────────────────────────────
print("\nD2: Variance ratio test per asset")
print(f"{'Asset':<8} {'var(σ_i-σ_E)':<15} {'var(σ_E)':<12} {'Ratio':>8} {'Support'}")
print("-"*52)
d2_results = {}
for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    sub = df.filter((pl.col("asset_symbol")==asset) & pl.col("sigma_ewma").is_not_null())
    if len(sub)<10: continue
    si = sub["sigma_implied"].to_numpy()
    se = sub["sigma_ewma"].to_numpy()
    resid_var = float(np.var(si-se))
    ewma_var  = float(np.var(se))
    ratio = resid_var/ewma_var if ewma_var>0 else float("nan")
    support = "B" if ratio<0.3 else ("A" if ratio>1.0 else "ambiguous")
    print(f"  {asset:<6} {resid_var:<15.5f} {ewma_var:<12.5f} {ratio:>8.3f}  {support}")
    d2_results[asset] = {"ratio":round(ratio,4),"support":support}

# ── D3: Sub-window stability ──────────────────────────────────────────────────
print("\nD3: Sub-window (6h) EWMA correlation stability")
df_pd = df.filter(pl.col("sigma_ewma").is_not_null()).to_pandas()
# Assign 6-hour bucket
df_pd["bucket6h"] = (df_pd["t_post_ns"].astype(float) / 1e9 / 3600).astype(int) // 6
corrs = []
for bucket, sub_b in df_pd.groupby("bucket6h"):
    if len(sub_b)<5: continue
    si = sub_b["sigma_implied"].values; se = sub_b["sigma_ewma"].values
    if np.std(se)<1e-9: continue
    r = float(np.corrcoef(si,se)[0,1])
    if np.isfinite(r): corrs.append(r)

corrs = np.array(corrs)
mean_corr = float(np.mean(corrs)); std_corr = float(np.std(corrs))
cv_corr = std_corr/abs(mean_corr) if abs(mean_corr)>1e-9 else float("nan")
print(f"  Buckets: {len(corrs)}, mean corr={mean_corr:.4f}, std={std_corr:.4f}, CV={cv_corr:.4f}")
d3_support = "B" if cv_corr<0.10 else ("A" if cv_corr>0.30 else "ambiguous")
print(f"  CV<0.10 → B, CV>0.30 → A: {d3_support}")

# ── D4: OOS R² for σ_EWMA → σ_implied ────────────────────────────────────────
print("\nD4: OOS prediction R²")
df_pd_sorted = df.filter(pl.col("sigma_ewma").is_not_null()).sort("t_post_ns").to_pandas()
n_tr = int(len(df_pd_sorted)*0.70)
tr_si = df_pd_sorted["sigma_implied"].values[:n_tr]
tr_se = df_pd_sorted["sigma_ewma"].values[:n_tr]
te_si = df_pd_sorted["sigma_implied"].values[n_tr:]
te_se = df_pd_sorted["sigma_ewma"].values[n_tr:]

# Fit OLS on train: σ_implied = a + b×σ_EWMA
A_ols = np.c_[np.ones(n_tr), tr_se]
w_ols,_,_,_ = lstsq(A_ols, tr_si, rcond=None)
pred_te = np.c_[np.ones(len(te_se)), te_se] @ w_ols
ss_res = float(np.sum((te_si-pred_te)**2))
ss_tot = float(np.sum((te_si-np.mean(te_si))**2))
r2_oos = float(1-ss_res/ss_tot) if ss_tot>0 else float("nan")
print(f"  OOS R²={r2_oos:.4f}  (train n={n_tr} test n={len(te_si)})")
print(f"  OLS fit: a={w_ols[0]:.4f}  b={w_ols[1]:.4f}")
d4_support = "B" if r2_oos>0.5 else ("A" if r2_oos<0.2 else "ambiguous")
print(f"  R²>0.5 → B, R²<0.2 → A: {d4_support}")

# Per-asset R²
print("  Per-asset:")
for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    sub = df_pd_sorted[df_pd_sorted["asset_symbol"]==asset]
    if len(sub)<20: continue
    n_a = int(len(sub)*0.7)
    si_t = sub["sigma_implied"].values[n_a:]; se_t = sub["sigma_ewma"].values[n_a:]
    si_tr = sub["sigma_implied"].values[:n_a]; se_tr = sub["sigma_ewma"].values[:n_a]
    w_a,_,_,_ = lstsq(np.c_[np.ones(n_a),se_tr], si_tr, rcond=None)
    pred_a = np.c_[np.ones(len(se_t)),se_t] @ w_a
    ss_r = float(np.sum((si_t-pred_a)**2)); ss_t = float(np.sum((si_t-np.mean(si_t))**2))
    r2_a = float(1-ss_r/ss_t) if ss_t>0 else float("nan")
    print(f"    {asset}: R²={r2_a:.4f}")

# ── D5: p_posted vs FV_EWMA plot ─────────────────────────────────────────────
print("\nD5: p_posted vs FV_EWMA")
fv_e = df.filter(pl.col("sigma_ewma").is_not_null())["fv_ewma"].to_numpy()
pp   = df.filter(pl.col("sigma_ewma").is_not_null())["p_posted"].to_numpy()
corr_fv = float(np.corrcoef(pp, fv_e)[0,1]) if len(fv_e)>2 else float("nan")
resid_d5 = pp - fv_e
print(f"  corr(p_posted, FV_EWMA) = {corr_fv:.4f}")
print(f"  residual (p_posted - FV_EWMA): mean={np.mean(resid_d5):.5f}  std={np.std(resid_d5):.5f}")
d5_support = "B" if abs(corr_fv)>0.85 else ("A" if abs(corr_fv)<0.5 else "ambiguous")
print(f"  |corr|>0.85 → B, |corr|<0.5 → A: {d5_support}")

# Plot
fig, axes = plt.subplots(1,2,figsize=(12,5))
axes[0].scatter(fv_e, pp, alpha=0.2, s=8)
axes[0].plot([0,1],[0,1],"r--",lw=1)
axes[0].set_xlabel("FV_EWMA"); axes[0].set_ylabel("p_posted")
axes[0].set_title(f"p_posted vs FV_EWMA  (r={corr_fv:.3f})")
axes[1].hist(resid_d5, bins=50, edgecolor="k", linewidth=0.3)
axes[1].axvline(0,color="r",lw=1)
axes[1].set_xlabel("p_posted - FV_EWMA")
axes[1].set_title(f"Residual  mean={np.mean(resid_d5):.4f}  std={np.std(resid_d5):.4f}")
fig.tight_layout()
fig.savefig(str(cfg.plots_dir/"pre7_pposted_vs_fvewma.png"), dpi=150)
plt.close()
print(f"  Plot saved: pre7_pposted_vs_fvewma.png")

# ── D6: Decision ─────────────────────────────────────────────────────────────
print("\n=== D6: DECISION ===")
support_votes = {"A":0,"B":0,"ambiguous":0}
test_summary = {}
for name, result in [("D2_BTC", d2_results.get("BTC",{}).get("support","ambiguous")),
                      ("D2_ETH", d2_results.get("ETH",{}).get("support","ambiguous")),
                      ("D2_SOL", d2_results.get("SOL",{}).get("support","ambiguous")),
                      ("D3_stability", d3_support),
                      ("D4_OOS_R2", d4_support),
                      ("D5_FV_corr", d5_support)]:
    support_votes[result] = support_votes.get(result,0)+1
    test_summary[name] = result
    print(f"  {name:<22}: {result}")

print(f"\n  Votes: B={support_votes['B']} A={support_votes['A']} ambiguous={support_votes['ambiguous']}")
if support_votes["B"] >= 3:
    DECISION = "B"
    print(f"  DECISION: Hypothesis B — EWMA σ + half-spread. Paper twin uses L2 parameters.")
elif support_votes["A"] >= 3:
    DECISION = "A"
    print(f"  DECISION: Hypothesis A — per-market σ. Paper twin needs σ identification step.")
else:
    DECISION = "mixed"
    print(f"  DECISION: Mixed — build both twins, compare match metrics.")

# ── Selection rule analysis ───────────────────────────────────────────────────
print("\n=== SELECTION RULE ANALYSIS ===")
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
quoted_markets = set(fills.filter(pl.col("market").is_not_null())["market"].str.to_lowercase().to_list())

gamma_cache = _load_cached_cids()
all_markets = set()
market_meta = {}
for k, meta in gamma_cache.items():
    cid = meta.get("condition_id","").lower()
    if cid:
        all_markets.add(cid)
        market_meta[cid] = meta

total_avail = len(all_markets)
total_quoted = len(quoted_markets & all_markets)
pct_quoted = 100*total_quoted/total_avail if total_avail>0 else 0
print(f"  Available markets in Gamma cache: {total_avail}")
print(f"  Markets ohanism quoted: {total_quoted} ({pct_quoted:.1f}%)")

# For each market with sigma_ewma available, classify quoted vs declined
sig_dict = {}
for row in df.iter_rows(named=True):
    mkt = str(row.get("market_id","")).lower()
    sig_dict[mkt] = row

quoted_stats = {"sigma_ewma":[], "otm_cushion":[], "hour_utc":[]}
declined_stats = {"sigma_ewma":[], "otm_cushion":[], "hour_utc":[]}

for mkt, meta in market_meta.items():
    row = sig_dict.get(mkt)
    if row is None or row["sigma_ewma"] is None: continue
    bucket = quoted_stats if mkt in quoted_markets else declined_stats
    oc = abs(float(row["p_posted"]) - 0.5)
    bucket["sigma_ewma"].append(row["sigma_ewma"])
    bucket["otm_cushion"].append(oc)
    bucket["hour_utc"].append(row["hour_utc"])

for fname, q, d in [("sigma_ewma", quoted_stats["sigma_ewma"], declined_stats["sigma_ewma"]),
                     ("otm_cushion", quoted_stats["otm_cushion"], declined_stats["otm_cushion"]),
                     ("hour_utc", quoted_stats["hour_utc"], declined_stats["hour_utc"])]:
    if not q or not d: continue
    qm = float(np.mean(q)); dm = float(np.mean(d))
    from scipy.stats import ttest_ind
    t_stat, p_val = ttest_ind(q, d)
    sig = "**SIGNIFICANT**" if p_val<0.05 else "not significant"
    print(f"  {fname:<15}: quoted={qm:.4f} declined={dm:.4f} p={p_val:.4f} {sig}")

print(f"\n  Quoted n={len(quoted_stats['sigma_ewma'])} Declined n={len(declined_stats['sigma_ewma'])}")

# ── Save results ──────────────────────────────────────────────────────────────
results = {
    "window_start": f"{WINDOW_START[0]} h{WINDOW_START[1]}",
    "window_end": f"{WINDOW_END[0]} h{WINDOW_END[1]}",
    "n_markets": int(len(df)),
    "n_with_ewma": int(n_ewma),
    "d2_variance_ratios": d2_results,
    "d3_corr_cv": round(cv_corr,4),
    "d3_support": d3_support,
    "d4_oos_r2": round(r2_oos,4),
    "d4_support": d4_support,
    "d5_corr_pposted_fvewma": round(corr_fv,4),
    "d5_residual_mean": round(float(np.mean(resid_d5)),5),
    "d5_residual_std": round(float(np.std(resid_d5)),5),
    "d5_support": d5_support,
    "test_summary": test_summary,
    "support_votes": support_votes,
    "decision": DECISION,
    "selection_rule": {
        "total_available_markets": total_avail,
        "ohanism_quoted": total_quoted,
        "pct_quoted": round(pct_quoted,1),
        "quoted_sigma_ewma_mean": round(float(np.mean(quoted_stats["sigma_ewma"])),4) if quoted_stats["sigma_ewma"] else None,
        "declined_sigma_ewma_mean": round(float(np.mean(declined_stats["sigma_ewma"])),4) if declined_stats["sigma_ewma"] else None,
    },
    "runtime_min": round((time.time()-t0)/60,2),
}
(cfg.results_dir / "pre7_disambiguate.json").write_text(json.dumps(results, indent=2))
print(f"\nRuntime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: output/ohanism/results/pre7_disambiguate.json")
