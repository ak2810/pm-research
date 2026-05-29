"""Pre-5.A: Resolve G6 by computing MTM from binary outcomes.

Primary path (A1.iii): ConditionResolution events from polygon data.
Fallback (A1.iii-Gamma): Gamma API market_resolved outcome per market.

ConditionResolution(conditionId, oracle, questionId, outcomeSlotCount, payoutNumerators):
  payoutNumerators=[1,0] → index 0 (Up token, clobTokenIds[0]) won
  payoutNumerators=[0,1] → index 1 (Down token) won

MTM = canonical_sign × (up_wins_binary - price_f) × size_f
where up_wins_binary ∈ {0, 1} from actual resolution (not Binance proxy)
"""
import sys
import json
import time
import ast
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np
import polars as pl
import requests

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
t0 = time.time()

DATES = ["2026-05-27","2026-05-28","2026-05-29"]
GAMMA_BASE = "https://gamma-api.polymarket.com"

# ── Step 1: Load ConditionResolution events from polygon ──────────────────────
print("Step 1: Loading ConditionResolution events from polygon...")
cond_res_rows = []
for date in DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False,
                             use_statistics=False)
        cr = lf.filter(pl.col("event") == "ConditionResolution").select(
            ["condition_id", "payout_numerators", "block_number", "t_recv_ns"]
        ).collect()
        if len(cr): cond_res_rows.append(cr)

if cond_res_rows:
    cond_df = pl.concat(cond_res_rows, how="diagonal_relaxed").unique(subset=["condition_id"])
    print(f"Found {len(cond_df)} unique ConditionResolution events")
else:
    cond_df = pl.DataFrame()
    print("No ConditionResolution events found in polygon data")

# ── Step 2: Parse payout_numerators to get up_wins ────────────────────────────
# payout_numerators is stored as a string like "[1, 0]" or "[0, 1]"
def parse_up_wins(pn_str) -> int | None:
    """Parse payoutNumerators string. Returns 1 if Up (index 0) wins, 0 if Down wins."""
    if pn_str is None:
        return None
    try:
        arr = json.loads(str(pn_str)) if isinstance(pn_str, str) else pn_str
        if isinstance(arr, list) and len(arr) >= 2:
            # payout=[1,0] → index 0 (Up) wins; payout=[0,1] → index 1 (Down) wins
            return 1 if arr[0] > 0 else 0
    except Exception:
        pass
    return None

if not cond_df.is_empty():
    cond_df = cond_df.with_columns(
        pl.col("payout_numerators").map_elements(
            lambda x: parse_up_wins(x), return_dtype=pl.Int32
        ).alias("up_wins")
    ).filter(pl.col("up_wins").is_not_null())
    print(f"Parsed outcomes: {len(cond_df)} markets")
    print(f"Up wins: {(cond_df['up_wins']==1).sum()}, Down wins: {(cond_df['up_wins']==0).sum()}")

# ── Step 3: Load Gamma cache for conditionId → market info ──────────────────────
from reverse_engineering.io.gamma import _load_cached_cids
gamma_cache = _load_cached_cids()
print(f"\nGamma cache: {len(gamma_cache)} entries")

# Build conditionId → {token_ids, asset_symbol, horizon} from Gamma cache
cid_to_market: dict[str, dict] = {}
for key, meta in gamma_cache.items():
    cid = meta.get("condition_id", "")
    if cid:
        try:
            tids = json.loads(meta.get("token_ids_json", "[]"))
        except: tids = []
        cid_to_market[cid.lower()] = {
            "token_ids": tids,  # [Up_token_id, Down_token_id]
            "asset_symbol": meta.get("asset_symbol",""),
            "horizon": meta.get("horizon",""),
        }

print(f"Markets in Gamma cache with conditionId: {len(cid_to_market)}")

# ── Step 4: Try Gamma API for markets not in polygon ConditionResolution ──────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
sig_v2 = sig_v2.unique(subset=["market_id"]).select(["market_id","S0"]).rename({"market_id":"market","S0":"strike"})

fills_w = fills.filter(
    pl.col("market").is_not_null() & pl.col("start_strike_price").is_not_null()
    & pl.col("t_block_ns").is_not_null() & pl.col("asset_symbol").is_not_null()
    & pl.col("time_to_expiry_s").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
])

# Add canonical_sign
fills_w = fills_w.with_columns(
    pl.when(
        ((pl.col("ohanism_side")=="BUY")&(pl.col("outcome_side")=="Up"))
        | ((pl.col("ohanism_side")=="SELL")&(pl.col("outcome_side")=="Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign")
).join(sig_v2, on="market", how="left")

# Build set of markets we need resolution for
needed_markets = set(fills_w["market"].drop_nulls().to_list())
print(f"\nFills need resolution for {len(needed_markets)} unique markets")

# ── Step 5: Join ConditionResolution to fills via conditionId ────────────────
# The "market" field in fills is the conditionId (0x-prefixed)
fill_market_lower = fills_w.with_columns(
    pl.col("market").str.to_lowercase().alias("cid_lower")
)

# From ConditionResolution polygon data
poly_outcomes: dict[str, int] = {}
if not cond_df.is_empty():
    for row in cond_df.iter_rows(named=True):
        cid = (row["condition_id"] or "").lower()
        uw = row["up_wins"]
        if cid and uw is not None:
            poly_outcomes[cid] = uw
print(f"Outcomes from polygon ConditionResolution: {len(poly_outcomes)}")

# ── Step 6: Gamma API fallback for missing resolutions ────────────────────────
missing_markets = needed_markets - set(poly_outcomes.keys())
print(f"Markets still needing resolution (trying Gamma API): {len(missing_markets)}")

gamma_outcomes: dict[str, int] = {}
for i, mkt in enumerate(list(missing_markets)[:200]):  # cap at 200 API calls
    try:
        url = f"{GAMMA_BASE}/events?slug={mkt}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200: continue
        data = r.json()
        if not data: continue
        ev = data[0]
        mkts = ev.get("markets", [])
        for m in mkts:
            if m.get("conditionId","").lower() == mkt.lower():
                winning_asset = m.get("winningAssetId") or m.get("winning_asset_id")
                tids = json.loads(m.get("clobTokenIds","[]")) if isinstance(m.get("clobTokenIds",""),str) else []
                if winning_asset and tids:
                    up_wins = 1 if (len(tids)>0 and tids[0] == winning_asset) else 0
                    gamma_outcomes[mkt.lower()] = up_wins
                elif m.get("closed") and m.get("outcomePrices"):
                    # Use outcome prices to determine winner
                    prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"],str) else m["outcomePrices"]
                    if prices and len(prices)>=2:
                        # Winner has price ≈ 1.0
                        up_wins = 1 if float(prices[0]) > 0.5 else 0
                        gamma_outcomes[mkt.lower()] = up_wins
        time.sleep(0.05)
    except Exception:
        pass

print(f"Additional outcomes from Gamma API: {len(gamma_outcomes)}")

# Combine all outcomes
all_outcomes = {**poly_outcomes, **gamma_outcomes}
print(f"Total outcomes resolved: {len(all_outcomes)} / {len(needed_markets)}")

# ── Step 7: Recompute MTM ─────────────────────────────────────────────────────
print("\nStep 7: Recomputing MTM from binary outcomes...")
pnl_rows = []
n_no_outcome = 0

for row in fills_w.iter_rows(named=True):
    mkt = (row["market"] or "").lower()
    canonical_sign = row["canonical_sign"]
    price_f = row["price_f"]
    size_f = row["size_f"]
    rebate_f = row["rebate_f"]

    # Look up binary outcome
    up_wins = all_outcomes.get(mkt)
    if up_wins is None:
        n_no_outcome += 1
        mtm_binary = float("nan")
    else:
        # MTM = canonical_sign × (up_wins - price_f) × size_f
        mtm_binary = float(canonical_sign * (up_wins - price_f) * size_f)

    pnl_rows.append({
        "asset": row.get("asset_symbol",""),
        "horizon": row.get("horizon",""),
        "canonical_sign": float(canonical_sign),
        "price": price_f,
        "size": size_f,
        "rebate": float(rebate_f) if rebate_f is not None else float("nan"),
        "mtm_binary": mtm_binary,
    })

pnl_df = pl.DataFrame(pnl_rows)
pnl_full = pnl_df.filter(pl.col("mtm_binary").is_finite())
print(f"Fills with binary MTM: {len(pnl_full)} / {len(pnl_df)} ({n_no_outcome} missing outcome)")

# ── Step 8: Aggregate and report ────────────────────────────────────────────────
print("\n=== PRE-5.A: CORRECTED PROFITABILITY DECOMPOSITION ===")
if len(pnl_full) > 0:
    total_rebate = float(pnl_full["rebate"].sum())
    total_mtm    = float(pnl_full["mtm_binary"].sum())
    total_as     = 0.0  # AS cost negligible per Phase 4.6 finding
    net_pnl      = total_rebate + total_mtm

    N = len(pnl_full)
    print(f"N={N:,} fills with complete P&L")
    print(f"  Rebate:       {total_rebate:+.2f} USDC  ({total_rebate/N:.4f}/fill)")
    print(f"  MTM (binary): {total_mtm:+.2f} USDC  ({total_mtm/N:.4f}/fill)")
    print(f"  AS:            ≈0 (negligible per Phase 4.6)")
    print(f"  Fees:          0.00 USDC (maker fills)")
    print(f"  NET P&L:      {net_pnl:+.2f} USDC  ({'POSITIVE ✓' if net_pnl>0 else 'NEGATIVE ✗'})")

    rebate_arr = pnl_full["rebate"].to_numpy()
    mtm_arr    = pnl_full["mtm_binary"].to_numpy()
    net_arr    = rebate_arr + mtm_arr

    print(f"\nPer-fill distributions:")
    print(f"  Rebate: mean={np.mean(rebate_arr):.4f} std={np.std(rebate_arr):.4f}")
    print(f"  MTM:    mean={np.mean(mtm_arr):.4f} std={np.std(mtm_arr):.4f}")
    print(f"  Net:    mean={np.mean(net_arr):.4f} std={np.std(net_arr):.4f}")
    print(f"  Fraction positive net: {(net_arr>0).mean()*100:.1f}%")

    print("\nPer-asset breakdown:")
    for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
        mask = pnl_full["asset"] == asset
        n_a = mask.sum()
        if n_a < 5: continue
        a_df = pnl_full.filter(pl.col("asset")==asset)
        r_a = float(a_df["rebate"].sum()); m_a = float(a_df["mtm_binary"].sum())
        print(f"  {asset}: n={n_a:,} rebate={r_a:+.1f} MTM={m_a:+.1f} net={(r_a+m_a):+.1f}")

    # G6 gate
    print(f"\n=== G6 GATE ===")
    if net_pnl > 0:
        print(f"G6 PASS ✓: Net P&L = {net_pnl:+.2f} USDC > 0")
    else:
        print(f"G6 FAIL: Net P&L = {net_pnl:+.2f} USDC < 0 → log BLOCKER-006")

    results = {
        "n": int(N), "n_missing_outcome": int(n_no_outcome),
        "total_rebate": round(total_rebate, 2), "total_mtm_binary": round(total_mtm, 2),
        "net_pnl": round(net_pnl, 2), "net_positive": bool(net_pnl > 0),
        "poly_outcomes_used": len(poly_outcomes),
        "gamma_outcomes_used": len(gamma_outcomes),
    }
    Path(str(cfg.results_dir / "pre5a_g6.json")).write_text(json.dumps(results, indent=2))
    print(f"\nSaved: output/results/pre5a_g6.json")

print(f"\nTotal runtime: {(time.time()-t0)/60:.1f} min")
