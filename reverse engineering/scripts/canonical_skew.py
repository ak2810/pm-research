"""Normalize SELL/BUY fills to canonical 'long Up' perspective (gotcha #2).

Gotcha #2: long Down == short Up. Normalize all positions:
- BUY Up  → +size (long Up)
- SELL Up → -size (short Up)
- BUY Down → -size (short Up, equivalent to short Up)
- SELL Down → +size (long Up, equivalent to long Up)

Then compute per-fill canonical_sign and aggregate by asset/horizon.
"""
import sys

sys.path.insert(0, "src")

import polars as pl

fills = pl.read_parquet("output/tables/ohanism_fills.parquet")

# Only analyze fills with outcome_side metadata (95.3%)
has_meta = fills.filter(
    pl.col("outcome_side").is_not_null() & pl.col("asset_symbol").is_not_null()
)
print(f"Fills with outcome_side: {len(has_meta)} / {len(fills)}")

# Build canonical_sign: +1 = increases long-Up exposure, -1 = decreases
# BUY Up  → +1    SELL Up  → -1
# BUY Down → -1   SELL Down → +1
has_meta = has_meta.with_columns([
    pl.when(
        ((pl.col("ohanism_side") == "BUY")  & (pl.col("outcome_side") == "Up"))
        | ((pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down"))
    )
    .then(pl.lit(1))
    .otherwise(pl.lit(-1))
    .alias("canonical_sign"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
])
has_meta = has_meta.with_columns(
    (pl.col("canonical_sign").cast(pl.Float64) * pl.col("size_f")).alias("net_long_up_tokens")
)

# ── Section 1: breakdown of SELL fills by outcome_side ──────────────────────
sell_fills = has_meta.filter(pl.col("ohanism_side") == "SELL")
buy_fills  = has_meta.filter(pl.col("ohanism_side") == "BUY")

print("\n=== SELL fills by outcome_side ===")
print(sell_fills["outcome_side"].value_counts())
print("\n=== BUY fills by outcome_side ===")
print(buy_fills["outcome_side"].value_counts())

# ── Section 2: canonical skew (all fills) ───────────────────────────────────
long_up   = has_meta.filter(pl.col("canonical_sign") == 1)
short_up  = has_meta.filter(pl.col("canonical_sign") == -1)

long_up_n     = long_up["size_f"].sum()
short_up_n    = short_up["size_f"].sum()
net_long_up   = has_meta["net_long_up_tokens"].sum()
total_notional = has_meta["size_f"].sum()

print("\n=== CANONICAL (long-Up normalized) SKEW ===")
print(f"Increases long-Up: {len(long_up):,} fills, {long_up_n:,.0f} tokens ({len(long_up)/len(has_meta)*100:.1f}%)")
print(f"Decreases long-Up: {len(short_up):,} fills, {short_up_n:,.0f} tokens ({len(short_up)/len(has_meta)*100:.1f}%)")
print(f"Net signed notional: {net_long_up:+,.0f} tokens ({net_long_up/total_notional*100:+.1f}% of gross)")
if abs(net_long_up / total_notional) < 0.05:
    print("VERDICT: Canonical skew ~SYMMETRIC (<5% net) — 83/17 was naming artifact.")
    print("  ohanism = rebate-maximizing one-sided MM, NOT directional.")
elif net_long_up > 0:
    pct = net_long_up / total_notional * 100
    print(f"VERDICT: LONG-UP bias ({pct:.1f}%) — real directional view. Phase 4.6/IRL pulled forward.")
else:
    pct = abs(net_long_up) / total_notional * 100
    print(f"VERDICT: SHORT-UP bias ({pct:.1f}%) — real directional view. Phase 4.6/IRL pulled forward.")

# ── Section 3: by asset ─────────────────────────────────────────────────────
print("\n=== CANONICAL SKEW BY ASSET ===")
asset_skew = (
    has_meta.group_by("asset_symbol")
    .agg([
        pl.col("net_long_up_tokens").sum().alias("net_long_up"),
        pl.col("size_f").sum().alias("total_notional"),
        pl.len().alias("fill_count"),
    ])
    .with_columns(
        (pl.col("net_long_up") / pl.col("total_notional") * 100).alias("net_pct")
    )
    .sort("fill_count", descending=True)
)
print(asset_skew)

# ── Section 4: by horizon ───────────────────────────────────────────────────
print("\n=== CANONICAL SKEW BY HORIZON ===")
horizon_skew = (
    has_meta.group_by("horizon")
    .agg([
        pl.col("net_long_up_tokens").sum().alias("net_long_up"),
        pl.col("size_f").sum().alias("total_notional"),
        pl.len().alias("fill_count"),
    ])
    .with_columns(
        (pl.col("net_long_up") / pl.col("total_notional") * 100).alias("net_pct")
    )
    .sort("fill_count", descending=True)
)
print(horizon_skew)

# ── Section 5: by asset × horizon ───────────────────────────────────────────
print("\n=== CANONICAL SKEW BY ASSET × HORIZON ===")
cross_skew = (
    has_meta.group_by(["asset_symbol", "horizon"])
    .agg([
        pl.col("net_long_up_tokens").sum().alias("net_long_up"),
        pl.col("size_f").sum().alias("total_notional"),
        pl.len().alias("fill_count"),
    ])
    .with_columns(
        (pl.col("net_long_up") / pl.col("total_notional") * 100).alias("net_pct")
    )
    .sort(["fill_count"], descending=True)
)
print(cross_skew)
