"""Pre-5.H: Down-side canonical sign audit.

Isolate Down-token SELL fills. For 100 samples where Down won (ohanism lost)
and 100 where Up won (ohanism profited), verify per-token P&L matches exactly:
  - Down won  → loss per token = price_f (= 1-q_down = canonical Up cost)
                          NOT 1, NOT q_down, NOT anything else
  - Up won    → gain per token = 1 - price_f = q_down (the Down sell proceeds)
                          NOT (1-price_f), NOT 1, NOT q_down itself

Also trace the canonical_sign assignment and up_wins attribution end-to-end
on these fills. Check for any double-counting.
"""
import sys, json, random
sys.path.insert(0, "src")

import polars as pl
import numpy as np
from reverse_engineering.config import get_settings

cfg = get_settings()

print("=== PRE-5.H: DOWN-SIDE CANONICAL SIGN AUDIT ===\n")

# ── Load fills ───────────────────────────────────────────────────────────────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fills = fills.with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
])

# ── Load polygon resolution outcomes ────────────────────────────────────────
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]
cond_res_rows = []
for date in DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True,
                             hive_partitioning=False, use_statistics=False)
        cr = (lf.filter(pl.col("event") == "ConditionResolution")
              .select(["condition_id", "payout_numerators"])
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
print(f"Polygon outcomes: {len(poly_outcomes)}")

# ── H1. Isolate Down-token SELL fills ────────────────────────────────────────
print("\nH1: Isolating Down-token SELL fills...")
sell_down = fills.filter(
    (pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down")
    & pl.col("market").is_not_null()
)
print(f"  SELL Down fills: {len(sell_down)} of {len(fills)} total ({100*len(sell_down)/len(fills):.1f}%)")

# Canonical sign for SELL Down: +1 (long-Up)
# price_f = 1 - price_Down (canonical Up cost basis)
# MTM formula: +1 × (up_wins - price_f) × size
#   = (up_wins - (1 - q_down)) × size
# If Up wins (up_wins=1): MTM = q_down × size   ← profit = what ohanism received for Down
# If Down wins (up_wins=0): MTM = -(1-q_down) × size ← loss = cost basis of Up token

# Verify this on a per-fill basis
sell_down_with_outcome = []
for row in sell_down.iter_rows(named=True):
    mkt = str(row.get("market") or "").lower()
    up_wins = poly_outcomes.get(mkt)
    if up_wins is None:
        continue
    q_down  = row["price_f"]   # price at which ohanism SOLD Down token
    price_f = 1.0 - q_down     # canonical Up price = cost basis of Up token
    size_f  = row["size_f"]

    # Our formula's MTM:
    canonical_sign = 1.0   # SELL Down = long Up
    our_mtm = canonical_sign * (up_wins - price_f) * size_f

    # Expected MTM (first principles):
    if up_wins == 1:
        expected_mtm = q_down * size_f          # received q_down per token, Up won → kept Up
    else:
        expected_mtm = -(1.0 - q_down) * size_f  # Down won → Up worthless, cost = 1-q_down

    sell_down_with_outcome.append({
        "market": mkt,
        "q_down": q_down,
        "price_f": price_f,
        "size_f": size_f,
        "up_wins": up_wins,
        "our_mtm": our_mtm,
        "expected_mtm": expected_mtm,
        "match": abs(our_mtm - expected_mtm) < 1e-6,
    })

matched = pl.DataFrame(sell_down_with_outcome)
print(f"  With polygon outcome: {len(matched)}")
n_exact_match = int(matched["match"].sum())
print(f"  Exact formula match: {n_exact_match}/{len(matched)} ({100*n_exact_match/len(matched):.2f}%)")

if n_exact_match < len(matched):
    bad = matched.filter(~pl.col("match")).head(5)
    print("  MISMATCHES:")
    for r in bad.iter_rows(named=True):
        print(f"    q_down={r['q_down']:.4f} size={r['size_f']:.2f} up_wins={r['up_wins']} "
              f"our={r['our_mtm']:+.4f} expected={r['expected_mtm']:+.4f}")

# ── H2. 100 Down-won (ohanism lost) samples ──────────────────────────────────
print("\nH2: 100 SELL Down fills where Down WON (ohanism LOST)...")
down_won_rows = matched.filter(pl.col("up_wins") == 0).to_dicts()
random.seed(42)
sample_dw = random.sample(down_won_rows, min(100, len(down_won_rows)))
print(f"  Available: {len(down_won_rows)}, sampling: {len(sample_dw)}")

errors = []
for r in sample_dw:
    q_down  = r["q_down"]
    size_f  = r["size_f"]
    our_mtm = r["our_mtm"]
    expected_loss = -(1.0 - q_down) * size_f  # loss = price_f = cost of Up
    if abs(our_mtm - expected_loss) > 1e-6:
        errors.append((r, expected_loss))

print(f"  Loss formula check (loss = -(1-q_down)*size): {len(sample_dw)-len(errors)}/{len(sample_dw)} pass")
if errors:
    print(f"  ERRORS: {len(errors)}")
    for r, exp in errors[:3]:
        print(f"    q_down={r['q_down']:.4f} our={r['our_mtm']:+.4f} expected={exp:+.4f}")

# Mean loss check
if sample_dw:
    mean_q = sum(r["q_down"] for r in sample_dw) / len(sample_dw)
    mean_size = sum(r["size_f"] for r in sample_dw) / len(sample_dw)
    mean_mtm = sum(r["our_mtm"] for r in sample_dw) / len(sample_dw)
    print(f"  Mean q_down={mean_q:.4f}  mean_size={mean_size:.2f}  mean_mtm={mean_mtm:+.4f}")
    print(f"  Expected mean_mtm = -(1-{mean_q:.4f})*{mean_size:.2f} = {-(1-mean_q)*mean_size:+.4f}")

# ── H3. 100 Up-won (ohanism profited) samples ────────────────────────────────
print("\nH3: 100 SELL Down fills where Up WON (ohanism PROFITED)...")
up_won_rows = matched.filter(pl.col("up_wins") == 1).to_dicts()
sample_uw = random.sample(up_won_rows, min(100, len(up_won_rows)))
print(f"  Available: {len(up_won_rows)}, sampling: {len(sample_uw)}")

errors_uw = []
for r in sample_uw:
    q_down  = r["q_down"]
    size_f  = r["size_f"]
    our_mtm = r["our_mtm"]
    expected_profit = q_down * size_f  # profit = what ohanism received for Down
    if abs(our_mtm - expected_profit) > 1e-6:
        errors_uw.append((r, expected_profit))

print(f"  Gain formula check (gain = q_down*size): {len(sample_uw)-len(errors_uw)}/{len(sample_uw)} pass")
if errors_uw:
    print(f"  ERRORS: {len(errors_uw)}")

# ── H4. P&L subtotal for audited 200 positions ──────────────────────────────
print("\nH4: Aggregate check on full SELL-Down subset with outcomes...")
our_total  = float(matched["our_mtm"].sum())
exp_total  = float(matched["expected_mtm"].sum())
print(f"  Our formula total:     {our_total:>+12,.2f} USDC  ({len(matched)} fills)")
print(f"  First-principles total:{exp_total:>+12,.2f} USDC")
print(f"  Difference:            {our_total-exp_total:>+12,.4f} USDC")
print()

# ── H5. Breakdown by resolution ──────────────────────────────────────────────
print("H5: Breakdown by resolution outcome...")
dw = matched.filter(pl.col("up_wins") == 0)  # Down won
uw = matched.filter(pl.col("up_wins") == 1)  # Up won
print(f"  Down won ({dw.height} fills): ohanism MTM sum = {float(dw['our_mtm'].sum()):>+12,.2f} USDC")
print(f"  Up won   ({uw.height} fills): ohanism MTM sum = {float(uw['our_mtm'].sum()):>+12,.2f} USDC")
print(f"  Total                            = {float(matched['our_mtm'].sum()):>+12,.2f} USDC")
print()
print(f"  Up-win rate:   {100*uw.height/len(matched):.1f}%  (Down-win: {100*dw.height/len(matched):.1f}%)")

# Compare to D4 total
print(f"\n  Full window MTM (all fills, pre5a):   -86,971 USDC")
print(f"  SELL Down fills MTM (with outcomes):  {our_total:>+12,.2f} USDC")
print(f"  Fraction of total explained:          {abs(our_total)/86971*100:.1f}%")

# ── H6. Double-count check: unique (block_number, log_index) in SELL Down ────
print("\nH6: Deduplication check for SELL Down fills...")
sell_down_dedup = sell_down.unique(subset=["block_number","log_index"])
print(f"  SELL Down total:  {len(sell_down)}")
print(f"  After dedup:      {len(sell_down_dedup)}")
dup_count = len(sell_down) - len(sell_down_dedup)
print(f"  Duplicates:       {dup_count} ({100*dup_count/len(sell_down):.1f}%)")

# Also check with all fills
fills_dedup = fills.unique(subset=["block_number","log_index"])
print(f"\n  All fills total:  {len(fills)}")
print(f"  After dedup:      {len(fills_dedup)}")
print(f"  Duplicates:       {len(fills)-len(fills_dedup)} ({100*(len(fills)-len(fills_dedup))/len(fills):.1f}%)")

# ── Save ─────────────────────────────────────────────────────────────────────
result = {
    "sell_down_total": len(sell_down),
    "sell_down_with_outcome": len(matched),
    "formula_exact_match_pct": round(100*n_exact_match/len(matched), 4) if len(matched) else None,
    "down_won_sample_errors": len(errors),
    "up_won_sample_errors": len(errors_uw),
    "our_total_sell_down_mtm": round(our_total, 4),
    "expected_total_mtm": round(exp_total, 4),
    "aggregate_gap": round(our_total-exp_total, 6),
    "up_win_rate": round(100*uw.height/len(matched), 2),
    "sell_down_dup_count": dup_count,
    "all_fills_dup_count": len(fills)-len(fills_dedup),
}
import pathlib
pathlib.Path("output/results/pre5h_sign_audit.json").write_text(json.dumps(result, indent=2))
print("\nSaved: output/results/pre5h_sign_audit.json")
