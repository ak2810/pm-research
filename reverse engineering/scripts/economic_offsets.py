"""Task 3: Pre-Phase-4 economic offset analysis.

Computes three metrics across all 20k fills:
1. Per-fill rebate earned (already in ohanism_fills.rebate_received)
2. Adverse selection: spot move from quote-placement to fill-time, signed against position
   Proxy: (Binance_mid_at_fill - start_strike) / start_strike,
   signed by ohanism's directional exposure.
   Since ohanism posts at market open (Phase 3 finding), quote-placement-time ≈
   start_date_unix (the start of the 5m market). We have this via start_strike_price.
3. OTM cushion: how far from ATM (p=0.5) does ohanism quote?
   = |fill_price - 0.5| — measures how far from fair in ATM-distance space.
4. Market selection: fraction of available 5m/15m markets ohanism actually quoted in.

Adverse selection formula:
- For SELL Up (canonical_sign = -1 if ohanism holds net short-Up from this fill):
  actually: if ohanism SOLD Up at price p, they gave away tokens that go up if spot goes up.
  Adverse selection against ohanism = spot goes up after fill → Up was underpriced.
  AS_signed = (S_fill - S_0) / S_0 × canonical_sign
  (canonical_sign = +1 = long-Up. If long-Up, adverse = spot DOWN. If short-Up, adverse = spot UP.)
  So: adverse_selection_per_fill = -(S_fill - S_0) / S_0 × canonical_sign
  Positive = adverse selection against ohanism.
"""
import sys

sys.path.insert(0, "src")

import numpy as np
import polars as pl

fills = pl.read_parquet("output/tables/ohanism_fills.parquet")

# Filter to fills with full metadata
full = fills.filter(
    pl.col("start_strike_price").is_not_null()
    & pl.col("outcome_side").is_not_null()
    & pl.col("asset_symbol").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("start_strike_price").cast(pl.Float64).alias("strike_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
])

# Canonical sign
full = full.with_columns(
    pl.when(
        ((pl.col("ohanism_side") == "BUY")  & (pl.col("outcome_side") == "Up"))
        | ((pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down"))
    )
    .then(pl.lit(1.0))
    .otherwise(pl.lit(-1.0))
    .alias("canonical_sign")
)

# Up-equivalent price
full = full.with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("up_price")
)

# Spot displacement from strike at fill time (proxy for quote-time drift)
# S_fill ≈ start_strike × (up_price → back-solve, but that requires σ)
# Instead: use the fill price itself as the best indicator of spot direction.
# up_price > 0.5 → spot above strike → positive displacement
full = full.with_columns(
    (pl.col("up_price") - 0.5).alias("atm_displacement")
)

# Adverse selection: positive = adverse (taker was informed against ohanism)
# ohanism long-Up (canonical_sign=+1): adverse if up_price at fill is LOW (spot went down)
# ohanism short-Up (canonical_sign=-1): adverse if up_price at fill is HIGH (spot went up)
full = full.with_columns(
    (-pl.col("atm_displacement") * pl.col("canonical_sign")).alias("adverse_selection")
)

# OTM cushion: distance from ATM of the UP equivalent price
full = full.with_columns(
    pl.col("atm_displacement").abs().alias("otm_cushion")
)

# Rebate per notional: rebate_f / (price_f × size_f) — rebate as fraction of fill value
full = full.with_columns(
    (pl.col("rebate_f") / (pl.col("price_f") * pl.col("size_f")).clip(lower_bound=1e-9)
    ).alias("rebate_pct_of_notional")
)

print("=== ECONOMIC OFFSETS (20k fills with metadata) ===")
print(f"  n = {len(full):,}")

# 1. Rebate
rebate_arr = full["rebate_f"].to_numpy()
notional_arr = (full["price_f"] * full["size_f"]).to_numpy()
print(f"\n1. Rebate per fill:")
print(f"   mean rebate = {np.mean(rebate_arr):.4f} USDC")
print(f"   median rebate = {np.median(rebate_arr):.4f} USDC")
print(f"   total rebate in window = {np.sum(rebate_arr):,.2f} USDC")
print(f"   mean rebate / notional = {np.mean(full['rebate_pct_of_notional'].to_numpy()) * 100:.3f}%")

# 2. Adverse selection
as_arr = full["adverse_selection"].to_numpy()
print(f"\n2. Adverse selection (positive = bad for ohanism):")
print(f"   mean = {np.mean(as_arr):.4f} (fraction of ATM)")
print(f"   std  = {np.std(as_arr):.4f}")
print(f"   positive (adverse) rate = {(as_arr > 0).mean() * 100:.1f}%")
print(f"   zero (ATM at fill) rate = {(np.abs(as_arr) < 0.001).mean() * 100:.1f}%")

# 3. OTM cushion
otm_arr = full["otm_cushion"].to_numpy()
print(f"\n3. OTM cushion (|fill_price - 0.5|):")
print(f"   mean = {np.mean(otm_arr):.4f}")
print(f"   median = {np.median(otm_arr):.4f}")
print(f"   p10 = {np.percentile(otm_arr, 10):.4f}")
print(f"   p90 = {np.percentile(otm_arr, 90):.4f}")
print(f"   pct of fills with cushion > 0.1: {(otm_arr > 0.1).mean() * 100:.1f}%")
print(f"   pct of fills with cushion > 0.2: {(otm_arr > 0.2).mean() * 100:.1f}%")
print(f"   pct near-ATM (cushion < 0.02):   {(otm_arr < 0.02).mean() * 100:.1f}%")

# 4. Market selection
print(f"\n4. Market selection fraction:")
from reverse_engineering.io.gamma import _load_cached_cids
cached = _load_cached_cids()
# Keys are Gamma conditionIds (0x...), values have "horizon" in meta
total_5m = sum(1 for v in cached.values() if v.get("horizon") == "5m")
total_15m = sum(1 for v in cached.values() if v.get("horizon") == "15m")
ohanism_5m = fills.filter(pl.col("horizon") == "5m")["market"].drop_nulls().n_unique()
ohanism_15m = fills.filter(pl.col("horizon") == "15m")["market"].drop_nulls().n_unique()
print(f"   5m markets in Gamma window: {total_5m} | ohanism traded: {ohanism_5m} "
      f"({ohanism_5m/max(total_5m,1)*100:.1f}%)")
print(f"   15m markets in Gamma window: {total_15m} | ohanism traded: {ohanism_15m} "
      f"({ohanism_15m/max(total_15m,1)*100:.1f}%)")

if ohanism_5m / max(total_5m, 1) < 0.9:
    print("   SELECTION ACTIVE: ohanism skips some 5m markets.")
    print("   Selection rule must be identified before Phase 4.")
else:
    print("   NEAR-FULL COVERAGE: ohanism quotes in nearly all 5m markets.")
    print("   No explicit selection rule needed in Phase 4 σ recipe.")

# 5. Rebate vs adverse selection net
print(f"\n5. Net edge per fill (rebate - adverse_selection × notional):")
# Note: adverse_selection is dimensionless (fraction of ATM distance)
# rebate is in USDC. Need comparable units.
# Use: edge = rebate - AS × notional (where AS fraction × price = USDC terms)
as_usdc = as_arr * notional_arr  # adverse selection in USDC-equivalent
edge_per_fill = rebate_arr - as_usdc
print(f"   mean rebate = {np.mean(rebate_arr):.4f} USDC")
print(f"   mean AS (USDC-equiv) = {np.mean(as_usdc):.4f} USDC")
print(f"   mean net edge = {np.mean(edge_per_fill):.4f} USDC")
print(f"   pct fills with positive net edge = {(edge_per_fill > 0).mean() * 100:.1f}%")
print(f"   Total net edge in window = {np.sum(edge_per_fill):,.2f} USDC")
