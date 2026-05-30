"""Phase 5 K1 — Full-window data build (standing data-window rule).

S1-S5 at top. Rebuild ohanism_fills_full on 84h window using Gamma cache
for market metadata (not pm_meta, which has poor 5m market coverage).
Then compute G6 on the full window.

Window: 2026-05-27/04 → 2026-05-30/16 (84 hours)
"""
import sys, json, time
sys.path.insert(0, "src")

import polars as pl
import numpy as np
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.gamma import _load_cached_cids
from reverse_engineering.tables.ohanism_fills import (
    extract_raw_fills, OHANISM_PROXY, CTF_V2, NEG_RISK_V2,
    _TAKER_FEE_RATE, _REBATE_RATE, _SIX_DP, _dec6, _BACKFILL_THRESHOLD_NS,
    OHANISM_FILLS_COLUMNS,
)
from reverse_engineering.io.block_times import fetch_block_times
from decimal import Decimal

cfg = get_settings()
t0 = time.time()

# ── S1-S5: Window enumeration ─────────────────────────────────────────────────
FEEDS = ["pm_clob", "polygon", "binance", "pm_meta"]
WINDOW_START = ("2026-05-27", 4)

feed_parts = {}
for feed in FEEDS:
    parts = sorted((p.date, p.hour) for p in list_local_partitions(feed))
    feed_parts[feed] = set(parts)

common = None
for feed in FEEDS:
    common = feed_parts[feed] if common is None else common & feed_parts[feed]

common = {p for p in common if p >= WINDOW_START}
common_sorted = sorted(common)
WINDOW_END = common_sorted[-1]
WINDOW_DATES = sorted(set(d for d, _ in common_sorted))

print(f"=== PHASE 5 K1: DATA BUILD ===")
print(f"Window: {WINDOW_START} → {WINDOW_END}")
print(f"Hours:  {len(common_sorted)}")
print(f"Dates:  {WINDOW_DATES}")

# ── Build Gamma token_id → market metadata lookup ─────────────────────────────
print("\nLoading Gamma cache for token metadata...")
gamma_cache = _load_cached_cids()
token2meta: dict[str, dict] = {}
for _key, meta in gamma_cache.items():
    cid = meta.get("condition_id", "")
    slug = meta.get("slug", "")
    asset = meta.get("asset_symbol", "")
    horizon = meta.get("horizon", "")
    end_date = meta.get("end_date_unix", None)
    start_date = meta.get("start_date_unix", None)
    try:
        tids = json.loads(meta.get("token_ids_json", "[]"))
    except Exception:
        tids = []
    for i, tid in enumerate(tids):
        if not cid or not tid:
            continue
        token2meta[str(tid)] = {
            "market": cid,
            "asset_symbol": asset,
            "horizon": horizon,
            "outcome_side": "Up" if i == 0 else "Down",
            "end_date_unix": float(end_date) if end_date else None,
            "start_date_unix": float(start_date) if start_date else None,
        }
print(f"  Gamma token mapping: {len(token2meta)} tokens → markets")

# ── Load all ohanism fills from polygon ───────────────────────────────────────
print("\nLoading ohanism fills from polygon...")
raw_dfs = []
all_block_numbers = []
for date in WINDOW_DATES:
    df = extract_raw_fills(date).collect()
    if len(df):
        raw_dfs.append(df)
        all_block_numbers.extend(df["block_number"].drop_nulls().to_list())
        print(f"  {date}: {len(df)} fills")

if not raw_dfs:
    raise RuntimeError("No fills found")

raw_all = pl.concat(raw_dfs)
raw_all = raw_all.unique(subset=["block_number", "log_index"], keep="first")
print(f"Total (deduped): {len(raw_all)}")

# ── Fetch block times ─────────────────────────────────────────────────────────
print("Fetching block times...")
unique_blocks = list(set(int(b) for b in all_block_numbers))
bt_map = fetch_block_times(unique_blocks)
bt_df = pl.DataFrame({
    "block_number": pl.Series(list(bt_map.keys()), dtype=pl.Int64),
    "t_block_ns": pl.Series(list(bt_map.values()), dtype=pl.Int64),
})
raw_all = raw_all.join(bt_df, on="block_number", how="left")
raw_all = raw_all.with_columns(
    ((pl.col("t_recv_ns") - pl.col("t_block_ns")).abs() > _BACKFILL_THRESHOLD_NS)
    .alias("is_backfilled")
)

# ── Enrich from Gamma token metadata ─────────────────────────────────────────
print("Enriching from Gamma metadata...")
_fee_factor = float(_REBATE_RATE * _TAKER_FEE_RATE)

rows = []
n_matched = 0
for row in raw_all.iter_rows(named=True):
    tid = str(row.get("token_id") or "")
    meta = token2meta.get(tid, {})
    market = meta.get("market")
    asset  = meta.get("asset_symbol", "")
    horizon = meta.get("horizon", "")
    outcome_side = meta.get("outcome_side")
    end_date_unix = meta.get("end_date_unix")
    start_date_unix = meta.get("start_date_unix")
    if market:
        n_matched += 1

    t_block_ns = row.get("t_block_ns")
    tau_s = (float(end_date_unix) - t_block_ns / 1e9) if (end_date_unix and t_block_ns) else None

    side = row.get("side", 0)
    ma = float(row.get("maker_amount_decimal") or 0)
    ta = float(row.get("taker_amount_decimal") or 0)
    if side == 0:  # taker BUY, ohanism SELL
        price_f = ma / ta if ta > 0 else None
        size_f  = ta
        ohanism_side = "SELL"
    else:           # taker SELL, ohanism BUY
        price_f = ta / ma if ma > 0 else None
        size_f  = ma
        ohanism_side = "BUY"

    fee_f = float(row.get("fee_decimal") or 0)

    rebate_f = None
    if price_f is not None and size_f is not None and (row.get("maker") == OHANISM_PROXY):
        rebate_f = _fee_factor * min(price_f, 1.0 - price_f) * size_f

    price_s  = _dec6(price_f) if price_f is not None else None
    size_s   = _dec6(size_f)  if size_f  is not None else None
    fee_s    = _dec6(fee_f)
    rebate_s = _dec6(rebate_f) if rebate_f is not None else _dec6(0.0)

    rows.append({
        "block_number":   int(row["block_number"]),
        "log_index":      int(row["log_index"]),
        "t_recv_ns":      int(row["t_recv_ns"]) if row.get("t_recv_ns") else None,
        "t_block_ns":     int(t_block_ns) if t_block_ns else None,
        "t_ws_ns":        int(t_block_ns) if t_block_ns else None,  # block_approx
        "t_ws_method":    "block_approx",
        "is_backfilled":  bool(row.get("is_backfilled") or False),
        "tx_hash":        str(row.get("tx_hash") or ""),
        "order_hash":     str(row.get("order_hash") or ""),
        "exchange":       str(row.get("exchange") or ""),
        "token_id":       tid,
        "market":         market,
        "asset_symbol":   asset or None,
        "horizon":        horizon or None,
        "is_maker":       row.get("maker") == OHANISM_PROXY,
        "ohanism_side":   ohanism_side,
        "outcome_side":   outcome_side,
        "price":          price_s,
        "size":           size_s,
        "fee_paid":       fee_s,
        "rebate_received": rebate_s,
        "time_to_expiry_s": tau_s,
        "start_strike_price": None,
        "builder":        str(row.get("builder") or ""),
        "metadata":       str(row.get("metadata") or ""),
    })

fills = pl.DataFrame(rows).sort(["block_number", "log_index"])
print(f"Enriched: {n_matched}/{len(fills)} fills have Gamma market metadata ({100*n_matched/len(fills):.1f}%)")

# Save fills
out_path = cfg.tables_dir / "ohanism_fills_full.parquet"
fills.write_parquet(str(out_path), compression="zstd")
print(f"Saved: {out_path} ({len(fills)} rows)")
print(f"Build time: {(time.time()-t0)/60:.1f} min")

# ── G6: Corrected MTM on full window ─────────────────────────────────────────
print("\n=== G6: CORRECTED MTM ON FULL 84H WINDOW ===")
print("Loading polygon outcomes...")
cond_res_rows = []
for date in WINDOW_DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False,
                             use_statistics=False)
        cr = (lf.filter(pl.col("event") == "ConditionResolution")
              .select(["condition_id", "payout_numerators"]).collect())
        if len(cr):
            cond_res_rows.append(cr)

cond_df = (pl.concat(cond_res_rows, how="diagonal_relaxed").unique(subset=["condition_id"])
           if cond_res_rows else pl.DataFrame())

def parse_up_wins(pn):
    if pn is None:
        return None
    try:
        arr = json.loads(str(pn))
        return 1 if (isinstance(arr, list) and len(arr) >= 2 and arr[0] > 0) else 0
    except Exception:
        return None

if not cond_df.is_empty():
    cond_df = cond_df.with_columns(
        pl.col("payout_numerators").map_elements(parse_up_wins, return_dtype=pl.Int32).alias("up_wins")
    ).filter(pl.col("up_wins").is_not_null())

poly_outcomes = {r["condition_id"].lower(): r["up_wins"] for r in cond_df.iter_rows(named=True)}
print(f"Polygon outcomes: {len(poly_outcomes)}")

fills_w = fills.filter(
    pl.col("market").is_not_null() & pl.col("price").is_not_null()
    & pl.col("size").is_not_null() & pl.col("outcome_side").is_not_null()
).with_columns([
    pl.when(pl.col("outcome_side") == "Down")
      .then(1.0 - pl.col("price").cast(pl.Float64))
      .otherwise(pl.col("price").cast(pl.Float64))
      .alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
]).with_columns(
    pl.when(
        ((pl.col("ohanism_side") == "BUY") & (pl.col("outcome_side") == "Up"))
        | ((pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign")
)
print(f"Fills with full metadata: {len(fills_w)}")

pnl_rows = []
for row in fills_w.iter_rows(named=True):
    mkt = (row["market"] or "").lower()
    up_wins = poly_outcomes.get(mkt)
    cs = row["canonical_sign"]
    pf = row["price_f"]
    sf = row["size_f"]
    rf = row["rebate_f"]
    mtm = float(cs * (up_wins - pf) * sf) if up_wins is not None else float("nan")
    pnl_rows.append({
        "asset": row.get("asset_symbol") or "",
        "horizon": row.get("horizon") or "",
        "canonical_sign": float(cs),
        "price_f": pf,
        "size_f": sf,
        "rebate": float(rf) if rf is not None else 0.0,
        "mtm": mtm,
        "up_wins": up_wins,
        "market": mkt,
    })

pnl_df = pl.DataFrame(pnl_rows)
pnl_full = pnl_df.filter(pl.col("mtm").is_finite())
N = len(pnl_full)
total_rebate = float(pnl_full["rebate"].sum())
total_mtm    = float(pnl_full["mtm"].sum())
net_pnl      = total_rebate + total_mtm

print(f"\nN={N:,} fills with complete P&L (of {len(fills_w):,} fills with metadata)")
print(f"  Rebate:   {total_rebate:>+12,.2f} USDC  ({total_rebate/N:.4f}/fill)")
print(f"  MTM:      {total_mtm:>+12,.2f} USDC  ({total_mtm/N:.4f}/fill)")
print(f"  Net P&L:  {net_pnl:>+12,.2f} USDC  ({'POSITIVE ✓' if net_pnl>0 else 'NEGATIVE ✗'})")
print()
for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
    sub = pnl_full.filter(pl.col("asset") == asset)
    if len(sub) < 5:
        continue
    r_a = float(sub["rebate"].sum()); m_a = float(sub["mtm"].sum())
    up_r = float((sub["up_wins"] == 1).sum()) / len(sub) * 100
    print(f"  {asset}: n={len(sub):,} rebate={r_a:+,.1f} MTM={m_a:+,.1f} net={(r_a+m_a):+,.1f} Up%={up_r:.1f}%")

g6_pass = net_pnl > 0
print(f"\nG6: {'PASS ✓' if g6_pass else 'FAIL'} — Net={net_pnl:+,.2f} USDC")

# Save pnl_df for Phase 5 GBT
pnl_df.write_parquet(str(cfg.results_dir / "phase5_pnl_base.parquet"), compression="zstd")

results = {
    "window_start": f"{WINDOW_START[0]} h{WINDOW_START[1]}",
    "window_end": f"{WINDOW_END[0]} h{WINDOW_END[1]}",
    "hours": len(common_sorted),
    "n_fills_total": len(fills),
    "n_fills_with_metadata": len(fills_w),
    "n_with_outcome": N,
    "total_rebate": round(total_rebate, 2),
    "total_mtm": round(total_mtm, 2),
    "net_pnl": round(net_pnl, 2),
    "g6_pass": g6_pass,
    "poly_outcomes": len(poly_outcomes),
}
(cfg.results_dir / "phase5_k1_window.json").write_text(json.dumps(results, indent=2))
print(f"\nTotal runtime: {(time.time()-t0)/60:.1f} min")
print(f"Saved: phase5_k1_window.json, phase5_pnl_base.parquet, ohanism_fills_full.parquet")
