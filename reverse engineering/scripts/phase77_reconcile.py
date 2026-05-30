"""Phase 7.7 Reconciliation — R1/R2.

R1: ohanism daily P&L distribution. Was OOT 55h a tail event or typical?
R2: Twin P&L on training period (earliest 60%). Compare train vs OOT ratios.
R3: Characterize Case A/B/C.
"""
import sys, json, time, pickle
sys.path.insert(0, "src")

import numpy as np
import polars as pl
from scipy.stats import norm
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.gamma import _load_cached_cids

cfg = get_settings()
t0 = time.time()
np.random.seed(42)

print("=== PHASE 7.7 RECONCILIATION ===")

FEEDS = ["pm_clob","polygon","binance","pm_meta"]
WINDOW_START = ("2026-05-27",4)
feed_parts = {f: set((p.date,p.hour) for p in list_local_partitions(f)) for f in FEEDS}
common = None
for f in FEEDS:
    common = feed_parts[f] if common is None else common & feed_parts[f]
common = {p for p in common if p >= WINDOW_START}
WINDOW_DATES = sorted(set(d for d,_ in sorted(common)))
SYMBOL_STREAM = {"BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","XRP":"XRPUSDT","DOGE":"DOGEUSDT"}
LAMBDA_EWMA = 0.94; BAR_S = 60; SEC_PER_YEAR = 365.25*24*3600; BARS_PER_YEAR = SEC_PER_YEAR/BAR_S
REBATE_FACTOR = 0.07*0.20; THETA_H0 = 0.0326; THETA_H1 = 0.5097
SUBMISSION_LAG_BASE_S = 129.0

# ── Load fills + outcomes ─────────────────────────────────────────────────────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fills_d = fills.unique(subset=["block_number","log_index"], keep="first")
ohanism_markets = set(fills_d.filter(pl.col("market").is_not_null())
                      ["market"].str.to_lowercase().to_list())

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

# ── Compute ohanism per-fill P&L with corrected price_f ─────────────────────
fills_w = fills_d.filter(
    pl.col("market").is_not_null() & pl.col("price").is_not_null()
    & pl.col("size").is_not_null() & pl.col("outcome_side").is_not_null()
    & pl.col("t_block_ns").is_not_null()
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

pnl_rows = []
for row in fills_w.iter_rows(named=True):
    mkt = (row["market"] or "").lower()
    up_wins = poly_outcomes.get(mkt)
    if up_wins is None: continue
    cs = row["canonical_sign"]; pf = row["price_f"]; sf = row["size_f"]
    rf = row["rebate_f"] or 0.0
    mtm = float(cs*(up_wins-pf)*sf)
    t_block = row["t_block_ns"]
    pnl_rows.append({
        "t_block_ns": int(t_block), "mtm": mtm, "rebate": rf, "net": mtm+rf,
        "day_utc": int(t_block)//1_000_000_000//86400,  # integer day number
        "hour_utc": (int(t_block)//1_000_000_000//3600)%24,
    })

pnl_df = pl.DataFrame(pnl_rows)
print(f"ohanism fills with P&L: {len(pnl_df)}")

# ── R1: ohanism daily P&L distribution ───────────────────────────────────────
print("\n=== R1: OHANISM P&L DISTRIBUTION ===")
pnl_daily = (pnl_df.group_by("day_utc")
             .agg(pl.col("net").sum().alias("daily_pnl"),
                  pl.len().alias("n_fills"))
             .sort("day_utc"))
daily_vals = pnl_daily["daily_pnl"].to_numpy()
print(f"Daily P&L (day = UTC calendar day):")
for row in pnl_daily.iter_rows(named=True):
    day = row["day_utc"]
    from datetime import datetime, timezone, timedelta
    dt = datetime.fromtimestamp(day*86400, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"  {dt}: {row['daily_pnl']:+,.1f} USDC  (n={row['n_fills']} fills)")

total_pnl_84h = float(pnl_df["net"].sum())
print(f"\nTotal 84h P&L: {total_pnl_84h:+,.1f} USDC")

# Hourly P&L for finer-grained distribution
pnl_hourly = (pnl_df.with_columns(
    (pl.col("t_block_ns")//1_000_000_000//3600).alias("hour_epoch")
).group_by("hour_epoch")
 .agg(pl.col("net").sum().alias("hourly_pnl"))
 .sort("hour_epoch"))
hourly_vals = pnl_hourly["hourly_pnl"].to_numpy()

# OOT period boundary: earliest 60% of gamma cache markets by start_date_unix
# Derive from the same sort used in phase77_oot_validation.py
_gamma_cache_ref = _load_cached_cids()
_start_dates = sorted([float(m.get("start_date_unix",0))
                       for m in _gamma_cache_ref.values()
                       if m.get("start_date_unix")])
N60 = int(len(_start_dates)*0.60)
OOT_CUTOFF_S = _start_dates[N60-1]  # start_date of last training market
OOT_START_NS = int(OOT_CUTOFF_S * 1e9)
OOT_DURATION_H = 55.0
from datetime import datetime, timezone
print(f"OOT cutoff (60th pct of markets): "
      f"{datetime.fromtimestamp(OOT_CUTOFF_S, tz=timezone.utc)}")

# ohanism OOT P&L over that period
pnl_oot_oh = float(pnl_df.filter(pl.col("t_block_ns") >= OOT_START_NS)["net"].sum())
pnl_train_oh = float(pnl_df.filter(pl.col("t_block_ns") < OOT_START_NS)["net"].sum())

n_train_h = len(pnl_df.filter(pl.col("t_block_ns") < OOT_START_NS))
n_oot_h   = len(pnl_df.filter(pl.col("t_block_ns") >= OOT_START_NS))
print(f"\nTrain period P&L: {pnl_train_oh:+,.1f} USDC  ({n_train_h} fills)")
print(f"OOT period P&L:   {pnl_oot_oh:+,.1f} USDC  ({n_oot_h} fills)")

# Distribution test: rolling 55h windows to see where OOT -1511 falls
WINDOW_NS = int(OOT_DURATION_H * 3600 * 1e9)
# Compute P&L per hour
pnl_hourly_dict = {int(r["hour_epoch"]): float(r["hourly_pnl"])
                   for r in pnl_hourly.iter_rows(named=True)}
min_hour = min(pnl_hourly_dict.keys())
max_hour = max(pnl_hourly_dict.keys())

rolling_55h = []
for h_start in range(min_hour, max_hour - 54):
    window_pnl = sum(pnl_hourly_dict.get(h, 0.0) for h in range(h_start, h_start+55))
    rolling_55h.append(window_pnl)

if rolling_55h:
    arr = np.array(rolling_55h)
    mean_55 = float(np.mean(arr)); std_55 = float(np.std(arr))
    z_oot = (pnl_oot_oh - mean_55) / std_55 if std_55 > 0 else float("nan")
    pct_worse = float(np.mean(arr <= pnl_oot_oh)) * 100

    print(f"\nRolling 55h P&L distribution ({len(rolling_55h)} windows):")
    print(f"  Mean: {mean_55:+,.1f} USDC")
    print(f"  Std:  {std_55:,.1f} USDC")
    print(f"  OOT actual: {pnl_oot_oh:+,.1f} USDC")
    print(f"  Z-score: {z_oot:.2f}")
    print(f"  Percentile: {pct_worse:.1f}% of 55h windows were worse")

    if abs(z_oot) <= 1.0:
        tail_characterization = "TYPICAL (within 1σ)"
    elif abs(z_oot) <= 2.0:
        tail_characterization = "BELOW AVERAGE (1-2σ)"
    elif z_oot < -2.0:
        tail_characterization = "TAIL BAD (>2σ below mean)"
    else:
        tail_characterization = "TAIL GOOD (>2σ above mean)"
    print(f"  Characterization: {tail_characterization}")
else:
    z_oot = float("nan"); mean_55 = float("nan"); std_55 = float("nan")
    tail_characterization = "INSUFFICIENT DATA"

# ── R2: Twin on training period ──────────────────────────────────────────────
print("\n=== R2: TWIN P&L ON TRAINING PERIOD ===")

# Load Binance EWMA
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
    bars_by_sym[sym] = (bar_ts[1:], bar_mid[1:], np.sqrt(ev*BARS_PER_YEAR))

def ewma_mid_at(sym, t_ns):
    if sym not in bars_by_sym: return 0.5, None
    ts, mids, sigs = bars_by_sym[sym]
    idx = np.searchsorted(ts, t_ns, side="right")-1
    if idx < 0: return 0.5, None
    return float(sigs[idx]), float(mids[idx])

# Load OOT classifier artifacts
clf_path = cfg.results_dir/"phase75_selection_clf.pkl"
with open(str(clf_path),"rb") as f:
    arts = pickle.load(f)
clf_orig = arts["clf"]; threshold_orig = arts["threshold"]
w_sz = arts["w_sz"]; prob_by_cid = arts["prob_by_cid"]

# Load OOT-trained classifier
oot_path = cfg.results_dir/"phase77_oot.json"
if oot_path.exists():
    oot_res = json.loads(oot_path.read_text())
    OOT_START_EPOCH = oot_res.get("n_train")  # not directly needed

# Rebuild feature rows time-sorted (same as OOT script)
gamma_cache = _load_cached_cids()
cid2se = {meta.get("condition_id","").lower():(float(meta.get("start_date_unix",0)),float(meta.get("end_date_unix",0)))
          for meta in gamma_cache.values() if meta.get("condition_id") and meta.get("start_date_unix")}
fills_ssp = (fills_d.filter(pl.col("market").is_not_null() & pl.col("start_strike_price").is_not_null())
             .group_by("market").agg(pl.col("start_strike_price").cast(pl.Float64).first().alias("S0"))
             .with_columns(pl.col("market").str.to_lowercase()))
ssp_map = {r["market"]:float(r["S0"]) for r in fills_ssp.iter_rows(named=True)}

all_rows_sorted = []
for meta in gamma_cache.values():
    cid = meta.get("condition_id","").lower(); asset = meta.get("asset_symbol","")
    sym = SYMBOL_STREAM.get(asset); se = cid2se.get(cid)
    if not all([cid,asset,sym,se]): continue
    start_ns = int(se[0]*1e9); end_ns = int(se[1]*1e9)
    tau_s = max((end_ns-start_ns)/1e9,1.0)
    sigma, S_t_open = ewma_mid_at(sym, start_ns)
    if S_t_open is None: continue
    S0 = ssp_map.get(cid, S_t_open)
    lag = min(SUBMISSION_LAG_BASE_S*np.sqrt(tau_s/300.0), tau_s*0.7)
    tau_rem_y = max(tau_s-lag,1.0)/SEC_PER_YEAR
    up_wins = poly_outcomes.get(cid)
    all_rows_sorted.append({
        "cid":cid,"asset":asset,"start_ns":start_ns,
        "sigma":sigma,"S0":S0,"S_t_open":S_t_open,
        "tau_rem_y":tau_rem_y,"lag":lag,"up_wins":up_wins,
        "ohanism_quoted":int(cid in ohanism_markets),
        "sel_prob":prob_by_cid.get(cid,0.5),
    })
all_rows_sorted.sort(key=lambda r: r["start_ns"])

# Training period: earliest 60%
N_total = len(all_rows_sorted)
N_train = int(N_total*0.60)
train_rows = all_rows_sorted[:N_train]
oot_rows   = all_rows_sorted[N_train:]
TRAIN_END_NS = train_rows[-1]["start_ns"]
print(f"Train rows: {N_train}, OOT rows: {N_total-N_train}")

# Run twin on TRAIN period
N_MC = 20
def run_twin_mc(rows, sel_threshold, n_mc=20):
    mc_pnls = []
    for seed in range(n_mc):
        rng = np.random.default_rng(seed)
        pnl = 0.0; n_sel = 0
        for m in rows:
            if m["sel_prob"] < sel_threshold: continue
            n_sel += 1
            sigma = m["sigma"]; S0 = m["S0"]; S_t_open = m["S_t_open"]
            lag = m["lag"]; tau_y = m["tau_rem_y"]
            w_sz_row = w_sz  # from original OLS (for comparability)
            pos_size = 330.0  # use constant for clean comparison
            z = float(rng.standard_normal())
            S_t_post = S_t_open * np.exp(sigma*np.sqrt(lag/SEC_PER_YEAR)*z)
            log_r = np.log(max(S0,1e-9)/max(S_t_post,1e-9))
            d = log_r/max(sigma*tau_y**0.5,1e-8)
            fv = float(1.0-norm.cdf(d))
            hs = THETA_H0+THETA_H1*sigma*tau_y**0.5
            p_q = float(np.clip(fv+(-1.0)*hs,0.01,0.99))
            rebate = min(p_q,1-p_q)*REBATE_FACTOR*pos_size
            up_wins = m["up_wins"]
            if up_wins is not None:
                pnl += 1.0*(up_wins-p_q)*pos_size + rebate
        mc_pnls.append(pnl)
    return float(np.mean(mc_pnls)), float(np.std(mc_pnls)), int(np.mean([
        sum(1 for m in rows if m["sel_prob"]>=sel_threshold) for _ in range(1)]))

# Use threshold_orig (from original classifier)
twin_train_pnl, twin_train_std, n_train_sel = run_twin_mc(train_rows, threshold_orig)
twin_oot_pnl, twin_oot_std, n_oot_sel = run_twin_mc(oot_rows, threshold_orig)

# ohanism P&L on train and OOT periods
oh_train_pnl = float(pnl_df.filter(pl.col("t_block_ns") < TRAIN_END_NS)["net"].sum())
oh_oot_pnl   = float(pnl_df.filter(pl.col("t_block_ns") >= TRAIN_END_NS)["net"].sum())

# How many ohanism markets in each period
oh_train_mkts = sum(1 for r in train_rows if r["ohanism_quoted"])
oh_oot_mkts   = sum(1 for r in oot_rows   if r["ohanism_quoted"])

print(f"\nTRAIN PERIOD (earliest 60%, n_markets={N_train}):")
print(f"  ohanism P&L: {oh_train_pnl:+,.1f} USDC  ({oh_train_mkts} markets)")
print(f"  twin P&L:    {twin_train_pnl:+,.1f} ± {twin_train_std:.0f} USDC  ({n_train_sel} markets)")
train_ratio = abs(twin_train_pnl/oh_train_pnl) if oh_train_pnl!=0 else float("nan")
train_direction = "OUTPERFORM" if twin_train_pnl > oh_train_pnl else "UNDERPERFORM"
if oh_train_pnl > 0:
    sign_match_train = "SAME DIRECTION (both positive)" if twin_train_pnl > 0 else "OPPOSITE"
elif oh_train_pnl < 0:
    sign_match_train = "SAME DIRECTION (both negative)" if twin_train_pnl < 0 else "OPPOSITE"
else:
    sign_match_train = "ohanism=0"
print(f"  Twin/ohanism ratio: {train_ratio:.2f}x  Direction: {sign_match_train}")

print(f"\nOOT PERIOD (latest 40%, n_markets={N_total-N_train}):")
print(f"  ohanism P&L: {oh_oot_pnl:+,.1f} USDC  ({oh_oot_mkts} markets)")
print(f"  twin P&L:    {twin_oot_pnl:+,.1f} ± {twin_oot_std:.0f} USDC  ({n_oot_sel} markets)")
oot_ratio = abs(twin_oot_pnl/oh_oot_pnl) if oh_oot_pnl!=0 else float("nan")
sign_match_oot = "SAME DIRECTION" if (twin_oot_pnl>0)==(oh_oot_pnl>0) else "OPPOSITE DIRECTION"
print(f"  Twin/ohanism ratio: {oot_ratio:.2f}x  {sign_match_oot}")

# ── R3: Case determination ────────────────────────────────────────────────────
print("\n=== R3: CASE DETERMINATION ===")
if oh_train_pnl > 0 and twin_train_pnl > 0:
    if train_ratio < 2.0 and oot_ratio >= 2.0:
        case = "A — twin ≈ ohanism in training, twin >> ohanism on OOT"
        interpretation = ("Recovered rule matches ohanism in typical conditions. "
                          "OOT outperformance is regime-conditional (down-market where "
                          "ohanism deviated from its usual rule).")
    elif train_ratio >= 2.0 and oot_ratio >= 2.0:
        case = "B — twin > ohanism on BOTH train and OOT"
        interpretation = ("Twin systematically outperforms. Recovered rule is genuinely "
                          "better than ohanism's noisy implementation across regimes.")
    else:
        case = f"MIXED — train ratio={train_ratio:.2f}x, OOT ratio={oot_ratio:.2f}x"
        interpretation = "Results mixed. Regime-conditional effect but not cleanly either Case A or B."
elif oh_train_pnl <= 0 and twin_train_pnl > 0:
    case = f"C — twin > ohanism on OOT, train comparison complex"
    interpretation = ("Both ohanism and twin have interesting results — need to examine "
                      "sign directions and magnitudes carefully.")
else:
    case = f"INDETERMINATE — train ratio={train_ratio:.2f}x, OOT ratio={oot_ratio:.2f}x"
    interpretation = "Cannot cleanly classify. Review raw numbers."

print(f"  Case: {case}")
print(f"  Tail characterization of OOT period: {tail_characterization} (z={z_oot:.2f})")
print(f"  Interpretation: {interpretation}")

# ── Summary for document ──────────────────────────────────────────────────────
print(f"\n=== SUMMARY FOR DOCUMENT UPDATE ===")
print(f"  ohanism 84h total P&L: {total_pnl_84h:+,.1f} USDC")
print(f"  ohanism train P&L: {oh_train_pnl:+,.1f} USDC")
print(f"  ohanism OOT P&L: {oh_oot_pnl:+,.1f} USDC")
print(f"  OOT P&L z-score vs rolling-55h distribution: {z_oot:.2f} ({tail_characterization})")
print(f"  Twin train P&L: {twin_train_pnl:+,.1f} USDC  (ratio vs ohanism: {train_ratio:.2f}x)")
print(f"  Twin OOT P&L: {twin_oot_pnl:+,.1f} USDC  (ratio vs ohanism: {oot_ratio:.2f}x)")
print(f"  Case: {case}")

# ── Save ──────────────────────────────────────────────────────────────────────
results = {
    "ohanism_84h_pnl": round(total_pnl_84h,2),
    "ohanism_train_pnl": round(oh_train_pnl,2),
    "ohanism_oot_pnl": round(oh_oot_pnl,2),
    "rolling_55h_mean": round(mean_55,2) if not np.isnan(mean_55) else None,
    "rolling_55h_std": round(std_55,2) if not np.isnan(std_55) else None,
    "oot_z_score": round(z_oot,3) if not np.isnan(z_oot) else None,
    "oot_tail_characterization": tail_characterization,
    "twin_train_pnl": round(twin_train_pnl,2),
    "twin_oot_pnl": round(twin_oot_pnl,2),
    "train_ratio": round(train_ratio,3) if not np.isnan(train_ratio) else None,
    "oot_ratio": round(oot_ratio,3) if not np.isnan(oot_ratio) else None,
    "case": case,
    "interpretation": interpretation,
    "runtime_min": round((time.time()-t0)/60,2),
}
(cfg.results_dir/"phase77_reconcile.json").write_text(json.dumps(results, indent=2))
print(f"\nRuntime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: output/ohanism/results/phase77_reconcile.json")
