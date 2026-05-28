"""Phase 2 analysis runner script."""
import sys
import time

sys.path.insert(0, "src")

import polars as pl

from reverse_engineering.tables.phase2_stats import run_phase2_analysis

fills = pl.read_parquet("output/tables/ohanism_fills.parquet")
print(f"Running Phase 2 on {len(fills)} fills...")
t0 = time.time()
results = run_phase2_analysis(fills)
print(f"Done in {time.time()-t0:.1f}s")

stats = results["first_order_stats"]
exp = results["exposure_stats"]
peak = results["peak_inventory_stats"]

print("\n=== PHASE 2 FIRST-ORDER STATS ===")
print(f"  maker_pct={stats['maker_pct']} sell_pct={stats['sell_pct']} buy_pct={stats['buy_pct']}")
print(f"  sell_notional={stats['sell_notional']:.0f} buy_notional={stats['buy_notional']:.0f}")
print(f"  unique_tokens={stats['unique_tokens']}")
fpt_med = stats["fills_per_token_median"]
fpt_p90 = stats["fills_per_token_p90"]
fpt_max = stats["fills_per_token_max"]
print(f"  fills/token: median={fpt_med:.1f} p90={fpt_p90:.0f} max={fpt_max}")
print(f"  direct_submission_pct={stats['direct_submission_pct']}")
pp = stats["price_percentiles"]
print(f"  price: p5={pp['p5']:.3f} p50={pp['p50']:.3f} p95={pp['p95']:.3f}")

print("\n=== EXPOSURE STATS ===")
print(f"  max={exp['max_exposure']:.0f} mean={exp['mean_exposure']:.0f}")
print(f"  p90={exp['p90_exposure']:.0f} p95={exp['p95_exposure']:.0f}")

print("\n=== PEAK INVENTORY PER MARKET ===")
print(f"  peak_abs: median={peak['median_peak_abs']:.1f} p90={peak['p90_peak_abs']:.1f}")
print(f"  net_zero_pct={peak['pct_net_zero']:.1f}")
print(f"  final_abs_median={peak['median_final_abs']:.4f}")
