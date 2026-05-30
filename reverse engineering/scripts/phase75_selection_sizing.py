"""Phase 7.5 — R1 Selection Rule + R2 Sizing Rule Recovery.

R1a: Per-market features at market open (available before ohanism's decision).
R1b: LightGBM classifier: ohanism_quoted ~ features. AUC determines approach.
R1c: If AUC>0.6, use classifier-predicted selection in twin.

R2a: Per-market total notional committed by ohanism.
R2b: Regress notional on open-time features.
R2c: Sizing rule for twin.

All under standing data-window rule.
"""
import sys, json, time
sys.path.insert(0, "src")

import numpy as np
import polars as pl
import lightgbm as lgb
import shap
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import norm, ttest_ind
from numpy.linalg import lstsq
from sklearn.metrics import roc_auc_score
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.gamma import _load_cached_cids

cfg = get_settings()
t0 = time.time()
np.random.seed(42)

print("=== PHASE 7.5: SELECTION RULE + SIZING RULE ===")

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
print(f"Window: {WINDOW_START} → {WINDOW_END} ({len(common_sorted)}h)")

SYMBOL_STREAM = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT","DOGE":"DOGEUSDT"}
LAMBDA_EWMA = 0.94; BAR_S = 60; SEC_PER_YEAR = 365.25*24*3600; BARS_PER_YEAR = SEC_PER_YEAR/BAR_S

# ── Build Binance 1-min bar EWMA + RV series ──────────────────────────────────
print("Building Binance EWMA + RV series...")
bticker_rows = []
for date in WINDOW_DATES:
    for pq in sorted(cfg.cache_dir.glob(f"feed=binance/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(pq), low_memory=True, hive_partitioning=False, use_statistics=False)
        df = (lf.filter(pl.col("e").is_null() & pl.col("b").is_not_null())
              .select(["t_recv_ns","s","b","a"]).collect())
        if len(df): bticker_rows.append(df)
bticker = (pl.concat(bticker_rows)
           .with_columns([pl.col("b").cast(pl.Float64).alias("bid"),
                          pl.col("a").cast(pl.Float64).alias("ask")])
           .with_columns(((pl.col("bid")+pl.col("ask"))/2).alias("mid"))
           .sort("t_recv_ns"))

bars_by_sym = {}
for sym in set(SYMBOL_STREAM.values()):
    sub = bticker.filter(pl.col("s")==sym).sort("t_recv_ns")
    if len(sub)<10: continue
    ts = sub["t_recv_ns"].to_numpy(); mid = sub["mid"].to_numpy()
    bar_label = ts // (BAR_S*10**9)
    _, fi = np.unique(bar_label, return_index=True)
    li = np.append(fi[1:]-1, len(ts)-1)
    bar_ts = ts[li]; bar_mid = mid[li]
    log_ret = np.log(bar_mid[1:]/np.maximum(bar_mid[:-1],1e-9))
    ev = np.zeros(len(log_ret)); ev[0] = log_ret[0]**2
    for i in range(1,len(log_ret)):
        ev[i] = LAMBDA_EWMA*ev[i-1] + (1-LAMBDA_EWMA)*log_ret[i]**2
    ewma_sig = np.sqrt(ev*BARS_PER_YEAR)
    bars_by_sym[sym] = (bar_ts[1:], bar_mid[1:], ewma_sig, log_ret)
print(f"  {len(bars_by_sym)} symbols")

def get_feats_at(sym, t_ns):
    """Returns (ewma_sig, mid, rv_1m, rv_5m, rv_30m) at t_ns."""
    if sym not in bars_by_sym: return None
    bar_ts, bar_mid, ewma_sig, log_ret = bars_by_sym[sym]
    idx = np.searchsorted(bar_ts, t_ns, side="right") - 1
    if idx < 30: return None
    return {
        "ewma_sig": float(ewma_sig[idx]),
        "mid": float(bar_mid[idx]),
        "rv_1m":  float(np.std(log_ret[max(0,idx-1):idx+1])*np.sqrt(BARS_PER_YEAR)),
        "rv_5m":  float(np.std(log_ret[max(0,idx-5):idx+1])*np.sqrt(BARS_PER_YEAR)),
        "rv_30m": float(np.std(log_ret[max(0,idx-30):idx+1])*np.sqrt(BARS_PER_YEAR)),
        "rv_60m": float(np.std(log_ret[max(0,idx-60):idx+1])*np.sqrt(BARS_PER_YEAR)),
        "ewma_pct_rank": float(np.mean(ewma_sig[max(0,idx-1440):idx+1] <= ewma_sig[idx]))
                          if idx > 1440 else 0.5,
    }

# ── Load ohanism fills + gamma metadata ──────────────────────────────────────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fills_d = fills.unique(subset=["block_number","log_index"], keep="first")
ohanism_markets = set(fills_d.filter(pl.col("market").is_not_null())
                      ["market"].str.to_lowercase().to_list())

# Per-market total notional (R2a)
mkt_notional = {}
fills_mkt = (fills_d.filter(pl.col("market").is_not_null()
                             & pl.col("price").is_not_null()
                             & pl.col("size").is_not_null())
             .with_columns([
                 pl.col("price").cast(pl.Float64).alias("pf"),
                 pl.col("size").cast(pl.Float64).alias("sf"),
             ])
             .with_columns((pl.col("pf")*pl.col("sf")).alias("notional")))
mkt_agg = (mkt_notional_df := fills_mkt.group_by("market").agg([
    pl.col("notional").sum().alias("total_notional"),
    pl.len().alias("n_fills"),
    pl.col("sf").sum().alias("total_size"),
]).with_columns(pl.col("market").str.to_lowercase()))
mkt_notional = {r["market"]: (r["total_notional"], r["n_fills"], r["total_size"])
                for r in mkt_agg.iter_rows(named=True)}

# ── R1a: Build per-market feature dataset ────────────────────────────────────
print("Building per-market features (R1a)...")
gamma_cache = _load_cached_cids()
cid2start_end = {}
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower()
    sd = meta.get("start_date_unix"); ed = meta.get("end_date_unix")
    if cid and sd and ed:
        cid2start_end[cid] = (float(sd), float(ed))

# Concurrent open exposure: for each market, count concurrent same-asset markets
# (pre-computed efficiently: for asset A at start_T, count markets of A that
# start_date < start_T AND end_date > start_T)
# Build per-asset sorted lists for binary search
from collections import defaultdict
asset_markets = defaultdict(list)  # asset → list of (start_ns, end_ns, cid)
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower()
    asset = meta.get("asset_symbol",""); horizon = meta.get("horizon","")
    if not all([cid,asset,horizon]): continue
    se = cid2start_end.get(cid)
    if se:
        asset_markets[asset].append((int(se[0]*1e9), int(se[1]*1e9)))

# Sort for binary search
for asset in asset_markets:
    asset_markets[asset].sort()

def count_concurrent(asset, start_ns):
    """Count markets of this asset that are open at start_ns."""
    markets = asset_markets.get(asset, [])
    cnt = sum(1 for s,e in markets if s < start_ns < e)
    return cnt

feature_rows = []
ASSET_ENC = {"BTC":0,"ETH":1,"SOL":2,"XRP":3,"DOGE":4}
HORIZON_ENC = {"5m":0,"15m":1,"1h":2}

n_feat = 0
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower()
    asset = meta.get("asset_symbol",""); horizon = meta.get("horizon","")
    if not all([cid,asset,horizon]): continue
    sym = SYMBOL_STREAM.get(asset)
    if not sym: continue
    se = cid2start_end.get(cid)
    if not se: continue
    start_ns = int(se[0]*1e9); end_ns = int(se[1]*1e9)
    tau_s = max((end_ns - start_ns)/1e9, 1.0)
    tau_y = tau_s / SEC_PER_YEAR

    feats = get_feats_at(sym, start_ns)
    if feats is None: continue

    concurrent = count_concurrent(asset, start_ns)
    hour_utc = (start_ns // 1_000_000_000 // 3600) % 24
    day_of_week = (start_ns // 1_000_000_000 // 86400) % 7

    feature_rows.append({
        "cid": cid, "asset": asset, "horizon": horizon,
        "ohanism_quoted": int(cid in ohanism_markets),
        "tau_y": tau_y, "tau_s": tau_s,
        "log_S0": float(np.log(max(feats["mid"],1e-9))),
        "sigma_ewma": feats["ewma_sig"],
        "rv_1m": feats["rv_1m"], "rv_5m": feats["rv_5m"],
        "rv_30m": feats["rv_30m"], "rv_60m": feats["rv_60m"],
        "ewma_pct_rank": feats["ewma_pct_rank"],
        "hour_utc": float(hour_utc), "day_of_week": float(day_of_week),
        "asset_enc": float(ASSET_ENC.get(asset,-1)),
        "horizon_enc": float(HORIZON_ENC.get(horizon,-1)),
        "concurrent_same_asset": float(concurrent),
    })
    n_feat += 1

df_feat = pl.DataFrame(feature_rows)
n_quoted = int((df_feat["ohanism_quoted"]==1).sum())
n_declined = int((df_feat["ohanism_quoted"]==0).sum())
print(f"  {len(df_feat)} markets: {n_quoted} quoted ({100*n_quoted/len(df_feat):.1f}%), "
      f"{n_declined} declined ({100*n_declined/len(df_feat):.1f}%)")

# ── R1b: LightGBM classifier ──────────────────────────────────────────────────
print("\nR1b: LightGBM classifier...")
FEATURES_CLS = ["tau_y","log_S0","sigma_ewma","rv_1m","rv_5m","rv_30m","rv_60m",
                 "ewma_pct_rank","hour_utc","day_of_week","asset_enc","horizon_enc",
                 "concurrent_same_asset"]

df_pd = df_feat.select(FEATURES_CLS + ["ohanism_quoted","cid"]).to_pandas().dropna()
X = df_pd[FEATURES_CLS].values
y = df_pd["ohanism_quoted"].values

# Temporal sort: sort by cid (proxy) then split 70/30
idx_sorted = np.argsort(df_pd.index.values)
n_tr = int(len(X) * 0.70)
tr_idx = idx_sorted[:n_tr]; te_idx = idx_sorted[n_tr:]
X_tr, y_tr = X[tr_idx], y[tr_idx]
X_te, y_te = X[te_idx], y[te_idx]

params_cls = {"objective":"binary","metric":"auc","num_leaves":15,
              "learning_rate":0.05,"feature_fraction":0.8,"bagging_fraction":0.8,
              "bagging_freq":5,"min_data_in_leaf":20,"verbose":-1,"random_state":42}
ds_tr = lgb.Dataset(X_tr, label=y_tr, feature_name=FEATURES_CLS)
ds_te = lgb.Dataset(X_te, label=y_te, feature_name=FEATURES_CLS, reference=ds_tr)
clf = lgb.train(params_cls, ds_tr, 300, valid_sets=[ds_te],
                callbacks=[lgb.early_stopping(40,verbose=False), lgb.log_evaluation(0)])

prob_te = clf.predict(X_te)
auc = float(roc_auc_score(y_te, prob_te))
print(f"  OOS AUC = {auc:.4f}")
if auc > 0.7:
    approach = "classifier"
    print(f"  AUC>0.7: selection rule is recoverable. Using classifier.")
elif auc > 0.6:
    approach = "classifier_weak"
    print(f"  AUC in [0.6,0.7]: weak but real signal. Using classifier with caution.")
else:
    approach = "random_064"
    print(f"  AUC<0.6: selection is ~random. Using 64.7% random participation.")

# SHAP top features
explainer_cls = shap.TreeExplainer(clf)
shap_cls = explainer_cls.shap_values(X_te)
mean_abs_cls = np.abs(shap_cls).mean(axis=0)
ranking_cls = np.argsort(mean_abs_cls)[::-1]
print(f"\n  Top 5 selection features:")
shap_sel_table = []
for rank, i in enumerate(ranking_cls[:5], 1):
    print(f"    {rank}. {FEATURES_CLS[i]:<25} |SHAP|={mean_abs_cls[i]:.4f}")
    shap_sel_table.append({"rank":rank,"feature":FEATURES_CLS[i],"mean_abs_shap":round(float(mean_abs_cls[i]),4)})

# Threshold for 64.7% participation
prob_all = clf.predict(X)
threshold_64 = float(np.percentile(prob_all, 100*(1-0.647)))
n_selected = int(np.sum(prob_all >= threshold_64))
print(f"\n  Threshold for 64.7% participation: {threshold_64:.4f} (selects {n_selected}/{len(X)} = {100*n_selected/len(X):.1f}%)")

# Save predicted probabilities per market
prob_by_cid = {cid: float(p) for cid,p in zip(df_pd["cid"].values, clf.predict(X))}

# ── R2a/R2b: Per-market sizing rule ──────────────────────────────────────────
print("\nR2: Sizing rule...")
sizing_rows = []
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower()
    if cid not in ohanism_markets: continue
    notional_data = mkt_notional.get(cid)
    if notional_data is None: continue
    total_notional, n_fills, total_size = notional_data

    asset = meta.get("asset_symbol",""); horizon = meta.get("horizon","")
    sym = SYMBOL_STREAM.get(asset)
    se = cid2start_end.get(cid)
    if not all([asset,horizon,sym,se]): continue

    start_ns = int(se[0]*1e9)
    feats = get_feats_at(sym, start_ns)
    if feats is None: continue

    tau_s = max((int(se[1]*1e9) - start_ns)/1e9, 1.0)
    tau_y = tau_s / SEC_PER_YEAR
    concurrent = count_concurrent(asset, start_ns)

    sizing_rows.append({
        "cid": cid, "asset": asset, "horizon": horizon,
        "total_notional": float(total_notional),
        "n_fills": int(n_fills),
        "total_size": float(total_size),
        "tau_y": tau_y, "tau_s": tau_s,
        "log_S0": float(np.log(max(feats["mid"],1e-9))),
        "sigma_ewma": feats["ewma_sig"],
        "rv_30m": feats["rv_30m"],
        "ewma_pct_rank": feats["ewma_pct_rank"],
        "hour_utc": float((start_ns//1_000_000_000//3600)%24),
        "asset_enc": float(ASSET_ENC.get(asset,-1)),
        "horizon_enc": float(HORIZON_ENC.get(horizon,-1)),
        "concurrent_same_asset": float(concurrent),
    })

df_sz = pl.DataFrame(sizing_rows)
print(f"  Sizing data: {len(df_sz)} quoted markets with notional data")
print(f"  Total notional: mean={float(df_sz['total_notional'].mean()):.1f}  "
      f"median={float(df_sz['total_notional'].median()):.1f}  "
      f"std={float(df_sz['total_notional'].std()):.1f}")
print(f"  Total size (tokens): mean={float(df_sz['total_size'].mean()):.1f}")

FEATURES_SZ = ["tau_y","log_S0","sigma_ewma","rv_30m","ewma_pct_rank",
               "asset_enc","horizon_enc","concurrent_same_asset","hour_utc"]

df_sz_pd = df_sz.select(FEATURES_SZ + ["total_size"]).to_pandas().dropna()
X_sz = df_sz_pd[FEATURES_SZ].values
y_sz = df_sz_pd["total_size"].values

n_tr2 = int(len(X_sz)*0.70)
w_sz,_,_,_ = lstsq(np.c_[np.ones(n_tr2),X_sz[:n_tr2]], y_sz[:n_tr2], rcond=None)
pred_sz_te = np.c_[np.ones(len(X_sz)-n_tr2),X_sz[n_tr2:]] @ w_sz
ss_res = float(np.sum((y_sz[n_tr2:]-pred_sz_te)**2))
ss_tot = float(np.sum((y_sz[n_tr2:]-np.mean(y_sz[n_tr2:]))**2))
r2_sz = float(1-ss_res/ss_tot) if ss_tot>0 else float("nan")
print(f"\n  OLS sizing R²={r2_sz:.4f}")
print(f"  Intercept={w_sz[0]:.1f}")
for i,f in enumerate(FEATURES_SZ):
    if abs(w_sz[i+1]) > 1.0:
        print(f"  {f:<25}: {w_sz[i+1]:+.2f}")

if r2_sz > 0.3:
    print("  R²>0.3: real sizing rule. Twin will use OLS predictions.")
    sizing_approach = "ols"
else:
    print(f"  R²={r2_sz:.3f}: weak sizing rule. Twin uses calibrated mean {float(df_sz['total_size'].mean()):.0f} tokens.")
    sizing_approach = "mean"

# Store sizing model
sz_mean = float(df_sz["total_size"].mean())
sz_std  = float(df_sz["total_size"].std())

# ── Save results ──────────────────────────────────────────────────────────────
results = {
    "selection": {
        "auc_oos": round(auc,4),
        "approach": approach,
        "threshold_for_647pct": round(threshold_64,4),
        "shap_top5": shap_sel_table,
    },
    "sizing": {
        "r2_ols": round(r2_sz,4),
        "approach": sizing_approach,
        "mean_tokens": round(sz_mean,1),
        "std_tokens": round(sz_std,1),
        "ols_intercept": round(float(w_sz[0]),2),
        "ols_coefs": {f: round(float(w_sz[i+1]),3) for i,f in enumerate(FEATURES_SZ)},
    },
    "n_markets_total": int(len(df_feat)),
    "n_quoted": int(n_quoted),
    "n_declined": int(n_declined),
    "runtime_min": round((time.time()-t0)/60,2),
}
out_path = cfg.results_dir / "phase75_selection_sizing.json"
out_path.write_text(json.dumps(results, indent=2))
print(f"\nRuntime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: {out_path}")

# Save classifier for use in twin
import pickle
clf_path = cfg.results_dir / "phase75_selection_clf.pkl"
with open(str(clf_path),"wb") as f:
    pickle.dump({"clf":clf,"features":FEATURES_CLS,"threshold":threshold_64,
                 "approach":approach,"w_sz":w_sz,"sizing_features":FEATURES_SZ,
                 "sizing_approach":sizing_approach,"sz_mean":sz_mean,
                 "prob_by_cid":prob_by_cid}, f)
print(f"Saved: {clf_path}")
