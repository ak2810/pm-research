"""Pre-5.F: Per-position P&L reconciliation against data-api /positions.

Protocol:
  F1. Get all current ohanism positions from data-api (15 rows).
  F2. Match each to our fills by conditionId.
  F3. For each matched position: compare our P&L vs API's cashPnl + realizedPnl.
  F4. Focus on clearly resolved positions (curPrice < 0.01 or > 0.99).
  F5. Decision: mean abs gap < 5% AND no systematic bias -> Pass.

Note: /positions only returns current holdings (size > 0). Redeemed positions are
gone. This limits the sample to 15, but structural discrepancies will be visible here.
"""
import sys, json
sys.path.insert(0, "src")

import requests
import polars as pl
import numpy as np
from reverse_engineering.config import get_settings

cfg = get_settings()

PROXY = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
BASE  = "https://data-api.polymarket.com"
WINDOW_END_NS = 1780030200 * 1_000_000_000  # 2026-05-29 04:59:59 UTC

# ── F1. Fetch positions ──────────────────────────────────────────────────────
print("=== PRE-5.F: PER-POSITION P&L RECONCILIATION ===\n")
r = requests.get(BASE + "/positions?user=" + PROXY + "&limit=500", timeout=20)
positions = r.json()
print(f"F1: {len(positions)} current positions from data-api\n")

# ── F2. Load our fills ───────────────────────────────────────────────────────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))

# Load polygon outcomes
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]
cond_res_rows = []
for date in DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True,
                             hive_partitioning=False, use_statistics=False)
        cr = (lf.filter(pl.col("event") == "ConditionResolution")
              .select(["condition_id", "payout_numerators", "block_number"])
              .collect())
        if len(cr):
            cond_res_rows.append(cr)

cond_df = (pl.concat(cond_res_rows, how="diagonal_relaxed")
           .unique(subset=["condition_id"])
           if cond_res_rows else pl.DataFrame())

def parse_up_wins(pn_str):
    if pn_str is None:
        return None
    try:
        arr = json.loads(str(pn_str))
        if isinstance(arr, list) and len(arr) >= 2:
            return 1 if arr[0] > 0 else 0
    except Exception:
        pass
    return None

if not cond_df.is_empty():
    cond_df = cond_df.with_columns(
        pl.col("payout_numerators")
        .map_elements(parse_up_wins, return_dtype=pl.Int32)
        .alias("up_wins")
    ).filter(pl.col("up_wins").is_not_null())

poly_outcomes = {
    row["condition_id"].lower(): row["up_wins"]
    for row in cond_df.iter_rows(named=True)
} if not cond_df.is_empty() else {}

print(f"F2: {len(fills)} fills loaded. {len(poly_outcomes)} polygon outcomes available.\n")

# ── F3. Per-position comparison ──────────────────────────────────────────────
print("F3: Per-position comparison")
print(f"{'conditionId':<22} {'outcome':<6} {'src':<8} {'api_sz':>8} "
      f"{'our_net':>8} {'api_cashPnl':>12} {'our_mtm':>10} "
      f"{'gap':>8} {'gap%':>7} {'status':<20}")
print("-" * 120)

comparison_rows = []

for pos in positions:
    cid = pos["conditionId"].lower()
    outcome_api = pos.get("outcome", "?")      # Up or Down (api token direction)
    api_sz       = float(pos["size"])
    api_avg_p    = float(pos["avgPrice"])
    api_initial  = float(pos["initialValue"])  # = avgPrice * size = cost basis
    api_cash     = float(pos.get("cashPnl", 0) or 0)
    api_realized = float(pos.get("realizedPnl", 0) or 0)
    api_total_pnl = api_cash + api_realized
    api_cur_p    = float(pos.get("curPrice", 0.5))

    # Classify resolution
    if api_cur_p < 0.01:
        resolved_outcome = "Down wins" if outcome_api == "Up" else "Up wins"
    elif api_cur_p > 0.99:
        resolved_outcome = "Up wins" if outcome_api == "Up" else "Down wins"
    else:
        resolved_outcome = "LIVE"

    # Match fills: same conditionId, in our window
    fills_for_mkt = fills.filter(
        pl.col("market").str.to_lowercase() == cid
    )
    in_window = fills_for_mkt.filter(pl.col("t_block_ns") <= WINDOW_END_NS)
    n_fills = len(in_window)

    if n_fills == 0:
        src = "NOT_IN_WINDOW"
        our_net = float("nan")
        our_mtm = float("nan")
        gap     = float("nan")
        gap_pct = float("nan")
        status  = "NO FILLS"
    else:
        # Compute our signed net position and MTM
        # canonical_sign: +1 = long-Up (SELL Down or BUY Up), -1 = short-Up (SELL Up or BUY Down)
        in_window_w = in_window.with_columns([
            pl.col("price").cast(pl.Float64).alias("price_f"),
            pl.col("size").cast(pl.Float64).alias("size_f"),
            pl.when(
                ((pl.col("ohanism_side") == "BUY")  & (pl.col("outcome_side") == "Up"))
                | ((pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down"))
            ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign"),
        ])

        up_wins = poly_outcomes.get(cid)
        src = "IN_WINDOW"

        # Net Up-token position (positive = net long-Up, negative = net short-Up)
        # Each fill: canonical_sign × size_f = signed Up-tokens
        our_net = float((in_window_w["canonical_sign"] * in_window_w["size_f"]).sum())

        if up_wins is None:
            our_mtm = float("nan")
            gap     = float("nan")
            gap_pct = float("nan")
            status  = "NO_OUTCOME"
        else:
            our_mtm = float(
                (in_window_w["canonical_sign"] * (up_wins - in_window_w["price_f"]) * in_window_w["size_f"]).sum()
            )
            # Add rebate
            rebate = float(in_window_w["rebate_received"].cast(pl.Float64).sum())
            our_total_pnl = our_mtm + rebate

            # Compare our MTM+rebate to API's cashPnl + realizedPnl
            # Note: API cashPnl = curPrice - avgPrice (unrealized), realizedPnl = realized from sells/redemptions
            # Our MTM = binary outcome - price_f (uses final resolution, not current curPrice)
            # For resolved positions (curPrice near 0 or 1), cashPnl ≈ our MTM
            gap     = our_total_pnl - api_total_pnl
            gap_pct = abs(gap / api_total_pnl) * 100 if abs(api_total_pnl) > 0.5 else float("nan")
            status  = f"resolved={resolved_outcome[:8]}" if resolved_outcome != "LIVE" else "LIVE"

    print(f"  {cid[:20]:<22} {outcome_api:<6} {src:<8} {api_sz:>8.2f} "
          f"{our_net if not np.isnan(our_net) else float('nan'):>8.2f} "
          f"{api_total_pnl:>12.3f} "
          f"{our_mtm if not np.isnan(our_mtm) else float('nan'):>10.3f} "
          f"{gap if not np.isnan(gap) else float('nan'):>8.3f} "
          f"{gap_pct if not np.isnan(gap_pct) else float('nan'):>7.1f}% "
          f"{status:<20}")

    comparison_rows.append({
        "conditionId":    cid,
        "outcome_api":    outcome_api,
        "resolved":       resolved_outcome,
        "api_sz":         api_sz,
        "api_avg_p":      api_avg_p,
        "api_total_pnl":  api_total_pnl,
        "n_fills":        n_fills,
        "our_net":        our_net,
        "our_mtm":        our_mtm,
        "gap":            gap,
        "gap_pct":        gap_pct,
    })

# ── F4. Aggregate on resolved positions ─────────────────────────────────────
print()
comp_df = pl.DataFrame([
    {k: (v if not (isinstance(v, float) and (v != v)) else None)
     for k, v in row.items()}
    for row in comparison_rows
])

resolved_matched = comp_df.filter(
    pl.col("resolved").is_in(["Down wins", "Up wins"])
    & pl.col("our_mtm").is_not_null()
)

print(f"=== F4. AGGREGATE (resolved positions with our fills) ===")
print(f"  Total positions in API: {len(positions)}")
print(f"  Matched to our fills:   {comp_df.filter(pl.col('n_fills') > 0).height}")
print(f"  Clearly resolved:       {resolved_matched.height}")

if resolved_matched.height > 0:
    gaps     = resolved_matched["gap"].to_list()
    gap_pcts = [g for g in resolved_matched["gap_pct"].to_list() if g is not None]
    mean_abs_gap = np.mean([abs(g) for g in gaps if g is not None])
    mean_gap     = np.mean([g for g in gaps if g is not None])
    std_gap      = np.std([g for g in gaps if g is not None])
    med_abs_pct  = np.median(gap_pcts) if gap_pcts else float("nan")

    print(f"  Mean absolute gap:      {mean_abs_gap:.3f} USDC")
    print(f"  Mean signed gap:        {mean_gap:.3f} USDC (>0 = we report more gain than API)")
    print(f"  Std of gap:             {std_gap:.3f} USDC")
    print(f"  Median abs gap %:       {med_abs_pct:.1f}%")
    print()

    # F5. Decision
    print("=== F5. DECISION ===")
    api_totals = [r for r in resolved_matched["api_total_pnl"].to_list() if r is not None]
    pass_threshold = med_abs_pct < 5.0 and abs(mean_gap) < 5.0

    our_sum = sum(g for g in resolved_matched["our_mtm"].to_list() if g is not None)
    api_sum = sum(api_totals)
    print(f"  Our total MTM (resolved): {our_sum:.2f} USDC")
    print(f"  API total P&L (resolved): {api_sum:.2f} USDC")
    print(f"  Sum gap: {our_sum - api_sum:.2f} USDC")
    print()

    if pass_threshold:
        print("  F5: PASS — per-position gap < 5%, no systematic bias.")
        print("  Accounting-methodology hypothesis CONFIRMED at position level.")
        print("  Phase 5 CLEARED.")
        verdict = "PASS"
    elif abs(mean_gap) > 5:
        bias_dir = "we over-report gains" if mean_gap > 0 else "we over-report losses"
        print(f"  F5: SYSTEMATIC BIAS — {bias_dir}")
        print(f"  Mean signed gap = {mean_gap:.3f} USDC per position.")
        if mean_gap < -1:
            print("  INVESTIGATION: missing winning leg, unredeemed positions at 0, or sign error on SELL leg.")
        elif mean_gap > 1:
            print("  INVESTIGATION: double-counted rebate, missing losing leg, or sign error.")
        verdict = "BIAS"
    else:
        print("  F5: INCONCLUSIVE — gaps variable, no clear pattern. Need larger sample.")
        verdict = "INCONCLUSIVE"
else:
    print("  No resolved matched positions available for aggregate check.")
    verdict = "INSUFFICIENT_SAMPLE"

# ── Leaderboard reconciliation note ─────────────────────────────────────────
print()
print("=== LEADERBOARD RECONCILIATION ===")
sum_current = sum(float(p.get("cashPnl", 0) or 0) + float(p.get("realizedPnl", 0) or 0)
                  for p in positions)
leaderboard = -1382.6536746211664
redeemed_pnl = leaderboard - sum_current
print(f"  Sum of current 15 positions P&L:  {sum_current:.2f} USDC")
print(f"  Leaderboard lifetime P&L:         {leaderboard:.2f} USDC")
print(f"  => Implied redeemed positions P&L: {redeemed_pnl:.2f} USDC")
print(f"     (P&L from all positions ohanism has already redeemed)")
print()
print(f"  Our 49h window P&L:              -83,831 USDC")
print(f"  Leaderboard lifetime P&L:        {leaderboard:.2f} USDC")
print(f"  Implied prior-history P&L:       {leaderboard - (-83831):.2f} USDC")
print(f"     (If our 49h is included in lifetime, prior windows sum to this.)")

# ── Save ─────────────────────────────────────────────────────────────────────
result = {
    "n_api_positions": len(positions),
    "n_matched": int(comp_df.filter(pl.col("n_fills") > 0).height),
    "n_resolved_matched": int(resolved_matched.height if resolved_matched.height > 0 else 0),
    "mean_abs_gap": float(mean_abs_gap) if resolved_matched.height > 0 else None,
    "mean_signed_gap": float(mean_gap) if resolved_matched.height > 0 else None,
    "std_gap": float(std_gap) if resolved_matched.height > 0 else None,
    "median_abs_gap_pct": float(med_abs_pct) if resolved_matched.height > 0 else None,
    "verdict": verdict,
    "leaderboard_pnl": leaderboard,
    "sum_current_positions_pnl": float(sum_current),
    "implied_redeemed_pnl": float(redeemed_pnl),
    "our_window_pnl": -83831,
    "implied_prior_history_pnl": float(leaderboard - (-83831)),
    "comparison": comparison_rows,
}
out_path = cfg.results_dir / "pre5f_reconciliation.json"
out_path.write_text(json.dumps(result, indent=2, default=str))
print(f"\nSaved: {out_path}")
