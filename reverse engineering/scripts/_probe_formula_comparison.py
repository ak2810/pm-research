"""Direct comparison: main-code formula (price_f=raw price) vs correct formula
(price_f = 1-price for Down fills) on the full SELL Down fill set.
Also compute corrected total P&L.
"""
import sys, json
sys.path.insert(0, "src")

import polars as pl
from reverse_engineering.config import get_settings

cfg = get_settings()
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fills = fills.with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
])
fills = fills.with_columns(
    pl.when(
        ((pl.col("ohanism_side")=="BUY")&(pl.col("outcome_side")=="Up"))
        | ((pl.col("ohanism_side")=="SELL")&(pl.col("outcome_side")=="Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign")
)

# Load polygon outcomes
DATES = ["2026-05-27","2026-05-28","2026-05-29"]
cond_res_rows = []
for date in DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True,
                             hive_partitioning=False, use_statistics=False)
        cr = (lf.filter(pl.col("event")=="ConditionResolution")
              .select(["condition_id","payout_numerators"]).collect())
        if len(cr): cond_res_rows.append(cr)

cond_df = (pl.concat(cond_res_rows, how="diagonal_relaxed")
           .unique(subset=["condition_id"])
           if cond_res_rows else pl.DataFrame())
def parse_up_wins(s):
    if s is None: return None
    try:
        arr = json.loads(str(s))
        return 1 if (isinstance(arr,list) and len(arr)>=2 and arr[0]>0) else 0
    except: return None

cond_df = cond_df.with_columns(
    pl.col("payout_numerators").map_elements(parse_up_wins, return_dtype=pl.Int32).alias("up_wins")
).filter(pl.col("up_wins").is_not_null())

poly_outcomes = {r["condition_id"].lower(): r["up_wins"] for r in cond_df.iter_rows(named=True)}
print(f"Polygon outcomes: {len(poly_outcomes)}")

# ── Per-fill: compute MAIN formula and CORRECT formula ───────────────────────
main_total_all   = 0.0
correct_total_all = 0.0
rebate_total     = 0.0
n_with_outcome   = 0

# Stratify by fill type
breakdown = {
    "SELL_Down": {"main":0., "correct":0., "n":0},
    "BUY_Up":    {"main":0., "correct":0., "n":0},
    "SELL_Up":   {"main":0., "correct":0., "n":0},
    "BUY_Down":  {"main":0., "correct":0., "n":0},
}

for row in fills.iter_rows(named=True):
    mkt = (row["market"] or "").lower()
    up_wins = poly_outcomes.get(mkt)
    if up_wins is None:
        continue

    n_with_outcome += 1
    cs    = row["canonical_sign"]
    price = row["price_f"]   # raw fill price stored in Parquet
    size  = row["size_f"]
    rebate= float(row["rebate_f"]) if row["rebate_f"] else 0.0
    rebate_total += rebate
    side    = row["ohanism_side"]
    outcome = row["outcome_side"]

    # Fill type
    fill_type = f"{side}_{outcome}"

    # MAIN formula: price_f = raw price (what pre5a actually does)
    mtm_main = cs * (up_wins - price) * size

    # CORRECT formula: price_f = canonical Up price
    #   For Up fills: price_f_canonical = price (it's already the Up token price)
    #   For Down fills: price_f_canonical = 1 - price (Down price → Up equivalent)
    if outcome == "Down":
        price_f_canonical = 1.0 - price
    else:
        price_f_canonical = price
    mtm_correct = cs * (up_wins - price_f_canonical) * size

    main_total_all    += mtm_main
    correct_total_all += mtm_correct

    if fill_type in breakdown:
        breakdown[fill_type]["main"]    += mtm_main
        breakdown[fill_type]["correct"] += mtm_correct
        breakdown[fill_type]["n"]       += 1

print(f"\n=== FORMULA COMPARISON ===")
print(f"{'Fill type':<12} {'n':>7} {'main_MTM':>14} {'correct_MTM':>14} {'diff':>12}")
print("-" * 65)
for k, v in breakdown.items():
    diff = v["correct"] - v["main"]
    print(f"  {k:<12} {v['n']:>7} {v['main']:>14,.2f} {v['correct']:>14,.2f} {diff:>12,.2f}")
print("-" * 65)
diff_total = correct_total_all - main_total_all
print(f"  {'TOTAL':<12} {n_with_outcome:>7} {main_total_all:>14,.2f} {correct_total_all:>14,.2f} {diff_total:>12,.2f}")
print()
print(f"Rebate (correct, same in both):  {rebate_total:>+12,.2f} USDC")
print()
print(f"=== NET P&L ===")
print(f"Main code:   MTM={main_total_all:>+12,.2f}  Net={main_total_all+rebate_total:>+12,.2f} USDC")
print(f"Correct:     MTM={correct_total_all:>+12,.2f}  Net={correct_total_all+rebate_total:>+12,.2f} USDC")
print()
print(f"=== G4 ARITHMETIC CHECK ===")
correct_net = correct_total_all + rebate_total
print(f"Corrected net P&L:        {correct_net:>+12,.2f} USDC")
print(f"External monthly P&L:         +173,508 USDC (top-5)")
print(f"Our 49h is {49}h of a ~30d month ({49/(30*24)*100:.1f}% of month by time)")
if correct_net > 0:
    monthly_rate = correct_net / (49/24) * 30  # extrapolate to 30 days
    print(f"Extrapolated monthly at this rate: {monthly_rate:>+12,.0f} USDC")
    print(f"External monthly / this rate: {173508/monthly_rate:.2f}x")

# Save
import pathlib, json as j
result = {
    "main_total_mtm": round(main_total_all, 2),
    "correct_total_mtm": round(correct_total_all, 2),
    "rebate_total": round(rebate_total, 2),
    "main_net_pnl": round(main_total_all + rebate_total, 2),
    "correct_net_pnl": round(correct_total_all + rebate_total, 2),
    "correction_total": round(diff_total, 2),
    "by_fill_type": {k: {kk: round(vv,2) if isinstance(vv,float) else vv
                         for kk,vv in v.items()} for k,v in breakdown.items()},
}
pathlib.Path("output/results/pre5h_formula_comparison.json").write_text(j.dumps(result, indent=2))
print(f"\nSaved: output/results/pre5h_formula_comparison.json")
