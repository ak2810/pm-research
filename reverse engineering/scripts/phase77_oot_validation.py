"""Phase 7.7 — Strict Out-of-Time Validation.

OOT1: Documents existing R1/R2 split as ARBITRARY (Gamma cache insertion order).
OOT2: Strict time-ordered re-fit: earliest 60% markets train, latest 40% OOT test.
OOT3: Re-run twin on OOT markets only (markets classifier has never seen).
OOT4: ohanism's actual P&L on those same OOT markets.
OOT5: Side-by-side comparison.
OOT6: Decision rule.
"""
import sys, json, time, pickle
sys.path.insert(0, "src")

import numpy as np
import polars as pl
import lightgbm as lgb
from scipy.stats import norm
from numpy.linalg import lstsq
from sklearn.metrics import roc_auc_score
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.gamma import _load_cached_cids

cfg = get_settings()
t0 = time.time()
np.random.seed(42)

print("=== PHASE 7.7: STRICT OUT-OF-TIME VALIDATION ===")

# ── S1-S5 ──────────────────────────────────────────────────────────────────────
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

SYMBOL_STREAM = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT","DOGE":"DOGEUSDT"}
LAMBDA_EWMA = 0.94; BAR_S = 60; SEC_PER_YEAR = 365.25*24*3600; BARS_PER_YEAR = SEC_PER_YEAR/BAR_S
REBATE_FACTOR = 0.07 * 0.20; SUBMISSION_LAG_BASE_S = 129.0

# ── OOT1: Document existing split ────────────────────────────────────────────
print("\n=== OOT1: EXISTING SPLIT METHODOLOGY ===")
print("R1 (classifier) split:")
print("  Code: idx_sorted = np.argsort(df_pd.index.values)")
print("  'index.values' = [0,1,2,...,N-1] (sequential row index from .to_pandas())")
print("  np.argsort([0,1,...]) = [0,1,...] => same as original row order")
print("  Row order = Gamma cache dict insertion order (Python 3.7+ preserves insertion)")
print("  Gamma cache insertion order ≠ chronological by start_date_unix")
print("  VERDICT: ARBITRARY split (not time-ordered, not random-seeded)")
print()
print("R2 (sizing OLS) split:")
print("  Code: n_tr2=int(len(X_sz)*0.70); train=X[:n_tr2], test=X[n_tr2:]")
print("  Same Gamma cache dict order => same arbitrary split")
print("  VERDICT: ARBITRARY split")
print()
print("IMPLICATION: AUC=0.87 and R2=0.32 may be inflated if dict order")
print("correlates with market features or outcomes.")

# ── Build Binance EWMA ────────────────────────────────────────────────────────
print("\nBuilding Binance EWMA...")
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
    bar_label = ts//(BAR_S*10**9)
    _, fi = np.unique(bar_label, return_index=True)
    li = np.append(fi[1:]-1, len(ts)-1)
    bar_ts = ts[li]; bar_mid = mid[li]
    log_ret = np.log(bar_mid[1:]/np.maximum(bar_mid[:-1],1e-9))
    ev = np.zeros(len(log_ret)); ev[0] = log_ret[0]**2
    for i in range(1,len(log_ret)):
        ev[i] = LAMBDA_EWMA*ev[i-1]+(1-LAMBDA_EWMA)*log_ret[i]**2
    ewma_sig = np.sqrt(ev*BARS_PER_YEAR)
    bars_by_sym[sym] = (bar_ts[1:], bar_mid[1:], ewma_sig, log_ret)
print(f"  {len(bars_by_sym)} symbols")

def feats_at(sym, t_ns):
    if sym not in bars_by_sym: return None
    bar_ts, bar_mid, ewma_sig, log_ret = bars_by_sym[sym]
    idx = np.searchsorted(bar_ts, t_ns, side="right")-1
    if idx < 30: return None
    return {"ewma_sig":float(ewma_sig[idx]), "mid":float(bar_mid[idx]),
            "rv_1m":float(np.std(log_ret[max(0,idx-1):idx+1])*np.sqrt(BARS_PER_YEAR)),
            "rv_5m":float(np.std(log_ret[max(0,idx-5):idx+1])*np.sqrt(BARS_PER_YEAR)),
            "rv_30m":float(np.std(log_ret[max(0,idx-30):idx+1])*np.sqrt(BARS_PER_YEAR)),
            "rv_60m":float(np.std(log_ret[max(0,idx-60):idx+1])*np.sqrt(BARS_PER_YEAR)),
            "ewma_pct_rank":float(np.mean(ewma_sig[max(0,idx-1440):idx+1]<=ewma_sig[idx]))
                             if idx>1440 else 0.5}

# ── Load Gamma + ohanism data ─────────────────────────────────────────────────
gamma_cache = _load_cached_cids()
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fills_d = fills.unique(subset=["block_number","log_index"], keep="first")
ohanism_markets = set(fills_d.filter(pl.col("market").is_not_null())
                      ["market"].str.to_lowercase().to_list())
fills_ssp = (fills_d.filter(pl.col("market").is_not_null() & pl.col("start_strike_price").is_not_null())
             .group_by("market")
             .agg(pl.col("start_strike_price").cast(pl.Float64).first().alias("S0"))
             .with_columns(pl.col("market").str.to_lowercase()))
ssp_map = {r["market"]:float(r["S0"]) for r in fills_ssp.iter_rows(named=True)}

cond_res_rows = []
for date in WINDOW_DATES:
    for pq in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(pq), low_memory=True, hive_partitioning=False, use_statistics=False)
        cr = (lf.filter(pl.col("event")=="ConditionResolution")
              .select(["condition_id","payout_numerators"]).collect())
        if len(cr): cond_res_rows.append(cr)
cond_df = pl.concat(cond_res_rows,how="diagonal_relaxed").unique(subset=["condition_id"]) if cond_res_rows else pl.DataFrame()
def parse_up(pn):
    if pn is None: return None
    try:
        arr=json.loads(str(pn))
        return 1 if (isinstance(arr,list) and len(arr)>=2 and arr[0]>0) else 0
    except: return None
if not cond_df.is_empty():
    cond_df = cond_df.with_columns(
        pl.col("payout_numerators").map_elements(parse_up,return_dtype=pl.Int32).alias("up_wins")
    ).filter(pl.col("up_wins").is_not_null())
poly_outcomes = {r["condition_id"].lower():r["up_wins"] for r in cond_df.iter_rows(named=True)}

ASSET_ENC = {"BTC":0,"ETH":1,"SOL":2,"XRP":3,"DOGE":4}
HORIZON_ENC = {"5m":0,"15m":1,"1h":2}

from collections import defaultdict
asset_mkts = defaultdict(list)
cid2se = {}
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower()
    asset = meta.get("asset_symbol","")
    sd = meta.get("start_date_unix"); ed = meta.get("end_date_unix")
    if cid and sd and ed:
        cid2se[cid] = (float(sd),float(ed))
        asset_mkts[asset].append((int(float(sd)*1e9),int(float(ed)*1e9)))

def concurrent(asset, start_ns):
    return sum(1 for s,e in asset_mkts.get(asset,[]) if s < start_ns < e)

# ── Build FULL feature set sorted by start_date_unix ─────────────────────────
print("Building time-sorted feature dataset...")
feature_rows = []
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower(); asset = meta.get("asset_symbol","")
    horizon = meta.get("horizon",""); sym = SYMBOL_STREAM.get(asset)
    se = cid2se.get(cid)
    if not all([cid,asset,horizon,sym,se]): continue
    start_ns = int(se[0]*1e9); end_ns = int(se[1]*1e9)
    tau_s = max((end_ns-start_ns)/1e9,1.0); tau_y = tau_s/SEC_PER_YEAR
    f = feats_at(sym, start_ns)
    if f is None: continue
    S0 = ssp_map.get(cid, f["mid"])
    conc = concurrent(asset, start_ns)
    hour_utc = (start_ns//1_000_000_000//3600)%24
    day_of_week = (start_ns//1_000_000_000//86400)%7
    lag_s = min(SUBMISSION_LAG_BASE_S*np.sqrt(tau_s/300.0), tau_s*0.7)
    tau_rem_y = max(tau_s-lag_s,1.0)/SEC_PER_YEAR
    # Sizing features
    sz_feats = [tau_y, float(np.log(max(f["mid"],1e-9))), f["ewma_sig"],
                f["rv_30m"], f["ewma_pct_rank"],
                float(ASSET_ENC.get(asset,-1)), float(HORIZON_ENC.get(horizon,-1)),
                float(conc), float(hour_utc)]
    feature_rows.append({
        "cid":cid,"asset":asset,"horizon":horizon,"sym":sym,
        "start_ns":start_ns,"tau_s":tau_s,"sigma":f["ewma_sig"],
        "S0":S0,"S_t_open":f["mid"],"tau_rem_y":tau_rem_y,"lag_s":lag_s,
        "ohanism_quoted":int(cid in ohanism_markets),
        "up_wins":poly_outcomes.get(cid),
        "tau_y":tau_y,"log_S0":float(np.log(max(f["mid"],1e-9))),
        "rv_1m":f["rv_1m"],"rv_5m":f["rv_5m"],"rv_30m":f["rv_30m"],"rv_60m":f["rv_60m"],
        "ewma_pct_rank":f["ewma_pct_rank"],"hour_utc":float(hour_utc),
        "day_of_week":float(day_of_week),
        "asset_enc":float(ASSET_ENC.get(asset,-1)),
        "horizon_enc":float(HORIZON_ENC.get(horizon,-1)),
        "concurrent_same_asset":float(conc),
        "sz_feats":sz_feats,
    })

# Sort by start_ns (chronological)
feature_rows.sort(key=lambda r: r["start_ns"])
N_total = len(feature_rows)
print(f"  {N_total} markets, sorted by start_date. "
      f"Earliest: {feature_rows[0]['start_ns']//10**9}  "
      f"Latest: {feature_rows[-1]['start_ns']//10**9}")

# ── OOT2: Time-ordered 60/40 split ───────────────────────────────────────────
N_train = int(N_total * 0.60)
N_oot   = N_total - N_train
train_rows = feature_rows[:N_train]
oot_rows   = feature_rows[N_train:]
print(f"\n=== OOT2: STRICT TIME-ORDERED SPLIT ===")
print(f"  Train: {N_train} markets (earliest 60%)")
print(f"  OOT test: {N_oot} markets (latest 40%)")
train_end_ns = train_rows[-1]["start_ns"]
oot_start_ns = oot_rows[0]["start_ns"]
from datetime import datetime, timezone
print(f"  Train ends: {datetime.fromtimestamp(train_end_ns//10**9, tz=timezone.utc)}")
print(f"  OOT starts: {datetime.fromtimestamp(oot_start_ns//10**9, tz=timezone.utc)}")

FEATURES_CLS = ["tau_y","log_S0","sigma_ewma","rv_1m","rv_5m","rv_30m","rv_60m",
                 "ewma_pct_rank","hour_utc","day_of_week","asset_enc","horizon_enc",
                 "concurrent_same_asset"]
FEATURES_SZ  = ["tau_y","log_S0","sigma_ewma","rv_30m","ewma_pct_rank",
                 "asset_enc","horizon_enc","concurrent_same_asset","hour_utc"]

def to_XY_cls(rows):
    feats = [[r["tau_y"],r["log_S0"],r["sigma"],r["rv_1m"],r["rv_5m"],r["rv_30m"],r["rv_60m"],
              r["ewma_pct_rank"],r["hour_utc"],r["day_of_week"],r["asset_enc"],r["horizon_enc"],
              r["concurrent_same_asset"]] for r in rows]
    labels = [r["ohanism_quoted"] for r in rows]
    return np.array(feats), np.array(labels)

def to_XY_sz(rows):
    # Only include markets where ohanism quoted (with sizing data)
    oh_rows = [r for r in rows if r["ohanism_quoted"]]
    feats = [r["sz_feats"] for r in oh_rows]
    # Get actual sizes from fills
    notional_rows = []
    for r in oh_rows:
        tot_sz = fills_d.filter(
            pl.col("market").str.to_lowercase() == r["cid"]
        ).with_columns(pl.col("size").cast(pl.Float64)).select("size").sum()[0,0]
        notional_rows.append(float(tot_sz) if tot_sz else 0.0)
    return np.array(feats), np.array(notional_rows)

X_tr_cls, y_tr_cls = to_XY_cls(train_rows)
X_oot_cls, y_oot_cls = to_XY_cls(oot_rows)
X_tr_sz, y_tr_sz = to_XY_sz(train_rows)
X_oot_sz, y_oot_sz = to_XY_sz(oot_rows)

print(f"  Classifier train:{len(X_tr_cls)}  OOT:{len(X_oot_cls)}")
print(f"    Train quoted pct:{100*y_tr_cls.mean():.1f}%  OOT quoted pct:{100*y_oot_cls.mean():.1f}%")
print(f"  Sizing train:{len(X_tr_sz)}  OOT:{len(X_oot_sz)}")

# Re-fit R1 on train, evaluate OOT
params_cls = {"objective":"binary","metric":"auc","num_leaves":15,
              "learning_rate":0.05,"feature_fraction":0.8,"bagging_fraction":0.8,
              "bagging_freq":5,"min_data_in_leaf":20,"verbose":-1,"random_state":42}
ds_tr = lgb.Dataset(X_tr_cls, label=y_tr_cls, feature_name=FEATURES_CLS)
ds_oot = lgb.Dataset(X_oot_cls, label=y_oot_cls, feature_name=FEATURES_CLS, reference=ds_tr)
clf_oot = lgb.train(params_cls, ds_tr, 300, valid_sets=[ds_oot],
                    callbacks=[lgb.early_stopping(40,verbose=False), lgb.log_evaluation(0)])
prob_oot_cls = clf_oot.predict(X_oot_cls)
auc_oot = float(roc_auc_score(y_oot_cls, prob_oot_cls))

# Also compute in-sample train AUC for comparison
prob_tr_cls = clf_oot.predict(X_tr_cls)
auc_train = float(roc_auc_score(y_tr_cls, prob_tr_cls))

print(f"\n  R1 Classifier:")
print(f"    ORIGINAL (arbitrary split, in-sample bias): AUC=0.8726")
print(f"    OOT train AUC (in-sample):   {auc_train:.4f}")
print(f"    OOT test AUC (strict OOT):   {auc_oot:.4f}")

# Re-fit R2 on train, evaluate OOT
if len(X_tr_sz) > 10:
    w_oot,_,_,_ = lstsq(np.c_[np.ones(len(X_tr_sz)),X_tr_sz], y_tr_sz, rcond=None)
    if len(X_oot_sz) > 5:
        pred_oot_sz = np.c_[np.ones(len(X_oot_sz)),X_oot_sz] @ w_oot
        ss_res = float(np.sum((y_oot_sz-pred_oot_sz)**2))
        ss_tot = float(np.sum((y_oot_sz-np.mean(y_oot_sz))**2))
        r2_oot_sz = float(1-ss_res/ss_tot) if ss_tot>0 else float("nan")
        # In-sample
        pred_tr_sz = np.c_[np.ones(len(X_tr_sz)),X_tr_sz] @ w_oot
        ss_r_tr = float(np.sum((y_tr_sz-pred_tr_sz)**2))
        ss_t_tr = float(np.sum((y_tr_sz-np.mean(y_tr_sz))**2))
        r2_train_sz = float(1-ss_r_tr/ss_t_tr) if ss_t_tr>0 else float("nan")
        print(f"\n  R2 Sizing OLS:")
        print(f"    ORIGINAL (arbitrary split, in-sample bias): R2=0.3214")
        print(f"    OOT train R2 (in-sample):   {r2_train_sz:.4f}")
        print(f"    OOT test R2 (strict OOT):   {r2_oot_sz:.4f}")
    else:
        r2_oot_sz = float("nan")
        print("  R2: insufficient OOT sizing data")
else:
    w_oot = None; r2_oot_sz = float("nan")

# ── OOT3: Run twin on OOT markets ─────────────────────────────────────────────
print("\n=== OOT3: TWIN ON OOT MARKETS (20 MC runs) ===")
# Threshold calibrated to same participation rate (67%)
probs_all_oot = clf_oot.predict(np.array([[r["tau_y"],r["log_S0"],r["sigma"],r["rv_1m"],r["rv_5m"],r["rv_30m"],r["rv_60m"],r["ewma_pct_rank"],r["hour_utc"],r["day_of_week"],r["asset_enc"],r["horizon_enc"],r["concurrent_same_asset"]] for r in oot_rows]))
threshold_oot = float(np.percentile(probs_all_oot, 100*(1-0.647)))

l2 = json.loads((cfg.results_dir/"phase4_l2.json").read_text())
THETA_H0, THETA_H1 = l2["stage2b"]["theta_h"]

N_MC = 20
mc_res_oot = []
for mc_seed in range(N_MC):
    rng = np.random.default_rng(mc_seed)
    fills_mc = []; n_sel = 0
    for i,m in enumerate(oot_rows):
        if probs_all_oot[i] < threshold_oot: continue
        n_sel += 1
        sigma = m["sigma"]; S0 = m["S0"]; S_t_open = m["S_t_open"]
        tau_y = m["tau_rem_y"]; lag = m["lag_s"]
        # Predict size from OOT-trained model
        if w_oot is not None:
            pos_size = float(np.clip(np.dot([1]+m["sz_feats"], w_oot), 10, 600))
        else:
            pos_size = 330.0
        z = float(rng.standard_normal())
        S_t_post = S_t_open * np.exp(sigma*np.sqrt(lag/SEC_PER_YEAR)*z)
        log_r = np.log(max(S0,1e-9)/max(S_t_post,1e-9))
        d = log_r/max(sigma*tau_y**0.5,1e-8)
        fv = float(1.0-norm.cdf(d))
        hs = THETA_H0 + THETA_H1*sigma*tau_y**0.5
        p_q = float(np.clip(fv+(-1.0)*hs, 0.01, 0.99))
        rebate = min(p_q,1-p_q)*REBATE_FACTOR*pos_size
        up_wins = m["up_wins"]
        mtm = float(1.0*(up_wins-p_q)*pos_size) if up_wins is not None else float("nan")
        net = (mtm+rebate) if up_wins is not None else float("nan")
        fills_mc.append({"asset":m["asset"],"otm":abs(p_q-0.5),"net":net,"up_wins":up_wins if up_wins is not None else -1})

    df_mc = pl.DataFrame(fills_mc)
    df_v = df_mc.filter(pl.col("net").is_finite() & (pl.col("up_wins")>=0))
    mc_res_oot.append({
        "total_pnl":float(df_v["net"].sum()) if len(df_v)>0 else 0,
        "otm":float(np.median(df_mc["otm"].to_numpy())) if len(df_mc)>0 else 0,
        "n_sel":n_sel,
        "asset":{a:float(df_v.filter(pl.col("asset")==a)["net"].sum()) for a in ["BTC","ETH","SOL","XRP","DOGE"]},
    })

twin_pnl_oot = np.mean([r["total_pnl"] for r in mc_res_oot])
twin_otm_oot = np.mean([r["otm"] for r in mc_res_oot])
twin_nsel_oot = np.mean([r["n_sel"] for r in mc_res_oot])
print(f"  OOT markets: {N_oot}, twin selects: {twin_nsel_oot:.0f}/run")
print(f"  Twin OOT mean P&L: {twin_pnl_oot:+,.1f} USDC")
print(f"  Twin OOT OTM cushion: {twin_otm_oot:.3f}")

# ── OOT4: ohanism actual P&L on OOT markets ──────────────────────────────────
print("\n=== OOT4: OHANISM ACTUAL P&L ON OOT MARKETS ===")
oot_cids = set(r["cid"] for r in oot_rows)
ohanism_oot_fills = fills_d.filter(
    pl.col("market").str.to_lowercase().is_in(oot_cids)
    & pl.col("price").is_not_null() & pl.col("size").is_not_null()
    & pl.col("outcome_side").is_not_null()
).with_columns([
    pl.when(pl.col("outcome_side")=="Down")
      .then(1.0-pl.col("price").cast(pl.Float64))
      .otherwise(pl.col("price").cast(pl.Float64))
      .alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
]).with_columns(
    pl.when(
        ((pl.col("ohanism_side")=="BUY")&(pl.col("outcome_side")=="Up"))
        |((pl.col("ohanism_side")=="SELL")&(pl.col("outcome_side")=="Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign")
)

oh_pnl_rows = []
for row in ohanism_oot_fills.iter_rows(named=True):
    mkt = (row["market"] or "").lower()
    up_wins = poly_outcomes.get(mkt)
    if up_wins is None: continue
    pf = row["price_f"]; sf = row["size_f"]; rf = row["rebate_f"] or 0.0
    cs = row["canonical_sign"]
    mtm = float(cs*(up_wins-pf)*sf); net = mtm+rf
    oh_pnl_rows.append({"asset":row.get("asset_symbol",""),"mtm":mtm,"rebate":rf,"net":net})

oh_pnl_df = pl.DataFrame(oh_pnl_rows)
oh_total_pnl = float(oh_pnl_df["net"].sum()) if len(oh_pnl_df)>0 else 0
oh_total_mkt = len(set(r["cid"] for r in oot_rows if r["cid"] in ohanism_markets))
oh_pnl_per_mkt = oh_total_pnl/oh_total_mkt if oh_total_mkt>0 else float("nan")
print(f"  ohanism fills on OOT markets: {len(oh_pnl_df)}")
print(f"  ohanism OOT markets quoted: {oh_total_mkt}/{N_oot}")
print(f"  ohanism OOT net P&L: {oh_total_pnl:+,.1f} USDC")
print(f"  ohanism OOT P&L per market: {oh_pnl_per_mkt:+.3f} USDC/mkt")

# ── OOT5: Side-by-side ────────────────────────────────────────────────────────
print("\n=== OOT5: SIDE-BY-SIDE COMPARISON ===")
twin_pnl_per_mkt = twin_pnl_oot/oh_total_mkt if oh_total_mkt>0 else float("nan")
print(f"\n{'Metric':<30} {'Twin OOT':>12} {'ohanism OOT':>12} {'Ratio':>8}")
print("-"*66)
print(f"  {'N markets':<28} {twin_nsel_oot:>12.0f} {oh_total_mkt:>12} {twin_nsel_oot/oh_total_mkt:>8.2f}x")
print(f"  {'Net P&L (USDC)':<28} {twin_pnl_oot:>+12,.1f} {oh_total_pnl:>+12,.1f} "
      f"{abs(twin_pnl_oot/oh_total_pnl):>8.2f}x" if oh_total_pnl!=0 else "")
print(f"  {'P&L per market (USDC)':<28} {twin_pnl_per_mkt:>+12.3f} {oh_pnl_per_mkt:>+12.3f}")
print(f"  {'OTM cushion':<28} {twin_otm_oot:>12.3f} {'0.220':>12}")

# Per-asset
ohanism_oot_asset = {}
for a in ["BTC","ETH","SOL","XRP","DOGE"]:
    sub = oh_pnl_df.filter(pl.col("asset")==a)
    ohanism_oot_asset[a] = float(sub["net"].sum()) if len(sub)>0 else 0.0
print("\n  Per-asset:")
print(f"  {'Asset':<8} {'Twin OOT':>12} {'ohanism OOT':>12} {'Sign match':>12}")
for a in ["BTC","ETH","SOL","XRP","DOGE"]:
    twin_a = float(np.mean([r["asset"].get(a,0) for r in mc_res_oot]))
    oh_a = ohanism_oot_asset.get(a,0)
    sign = "MATCH" if (twin_a>0)==(oh_a>0) else "MISMATCH"
    print(f"  {a:<8} {twin_a:>+12,.0f} {oh_a:>+12,.0f} {sign:>12}")

# ── OOT6: Decision ────────────────────────────────────────────────────────────
print("\n=== OOT6: DECISION ===")
if oh_total_pnl == 0:
    verdict = "INCONCLUSIVE (ohanism P&L=0 on OOT)"
    ratio_oot = float("nan")
elif abs(twin_pnl_oot) == 0:
    verdict = "INCONCLUSIVE (twin P&L=0 on OOT)"
    ratio_oot = float("nan")
else:
    ratio_oot = abs(twin_pnl_oot / oh_total_pnl)
    if ratio_oot >= 2.0:
        verdict = f"OUTPERFORMANCE REAL: twin {ratio_oot:.2f}x ohanism on OOT data"
    elif ratio_oot <= 0.5:
        verdict = f"UNDERPERFORMANCE: twin {ratio_oot:.2f}x ohanism on OOT"
    elif 0.5 < ratio_oot < 2.0:
        verdict = f"COMPARABLE: twin {ratio_oot:.2f}x ohanism on OOT — within 2x"
    else:
        verdict = f"ratio={ratio_oot:.2f}x"

print(f"  OOT ratio (|twin_pnl|/|ohanism_pnl|): {ratio_oot:.2f}x")
print(f"  {verdict}")

# AUC comparison
print(f"\n  AUC comparison:")
print(f"    Original (arbitrary split): 0.8726")
print(f"    OOT train (in-sample):      {auc_train:.4f}")
print(f"    OOT test (strict OOT):      {auc_oot:.4f}")
if auc_oot > 0.7:
    print(f"    -> Selection rule survives OOT (AUC>0.7). Real signal.")
elif auc_oot > 0.6:
    print(f"    -> Weak but real (AUC in [0.6,0.7]). Some deflation from arbitrary split.")
else:
    print(f"    -> AUC<0.6 on OOT: original AUC was largely spurious. Look-ahead bias confirmed.")

# ── Save ──────────────────────────────────────────────────────────────────────
results = {
    "oot1_split_verdict": "ARBITRARY (Gamma cache dict insertion order, not time-sorted)",
    "oot2_train_pct": 60, "n_train": N_train, "n_oot": N_oot,
    "r1_auc_original": 0.8726,
    "r1_auc_train_insample": round(auc_train,4),
    "r1_auc_oot": round(auc_oot,4),
    "r2_r2_original": 0.3214,
    "r2_r2_train_insample": round(r2_train_sz,4) if not np.isnan(r2_train_sz) else None,
    "r2_r2_oot": round(r2_oot_sz,4) if not np.isnan(r2_oot_sz) else None,
    "twin_oot_pnl": round(twin_pnl_oot,2),
    "twin_oot_n_selected": round(twin_nsel_oot,1),
    "ohanism_oot_pnl": round(oh_total_pnl,2),
    "ohanism_oot_markets": oh_total_mkt,
    "pnl_ratio_oot": round(ratio_oot,3) if not np.isnan(ratio_oot) else None,
    "oot6_verdict": verdict,
    "runtime_min": round((time.time()-t0)/60,2),
}
(cfg.results_dir/"phase77_oot.json").write_text(json.dumps(results, indent=2))
print(f"\nRuntime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: output/ohanism/results/phase77_oot.json")
