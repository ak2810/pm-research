"""Test whether XRP 5m long-Up bias is mechanical (rebate) or alpha.

Mechanism hypothesis: ohanism quotes the lower-priced (OTM) token to maximize
rebate = 0.014 * min(p, 1-p) * size. When Up > 0.5, Down is OTM → SELL Down
= long Up. The XRP 31.7% long-Up bias is mechanical IFF XRP Up tokens are
disproportionately priced > 0.5 during our window.

Test:
1. For each XRP 5m fill, compute which token was 'rebate-favored' at fill time.
   fill price p → rebate-favored = Up if p < 0.5 else Down.
2. Compare ohanism's actual quoting side to rebate-favored side.
3. If ohanism always (or nearly always) quotes rebate-favored side → pure mechanism.
4. If they deviate systematically → alpha overlay.

Also: check price distribution for XRP vs other assets. If XRP Up tokens
typically price > 0.5 in this window, the long-Up bias is mechanically explained.
"""
import sys

sys.path.insert(0, "src")

import numpy as np
import polars as pl

fills = pl.read_parquet("output/tables/ohanism_fills.parquet")
has_meta = fills.filter(
    pl.col("outcome_side").is_not_null() & pl.col("asset_symbol").is_not_null()
)

print(f"Fills with metadata: {len(has_meta)}")

# Canonical sign per fill
has_meta = has_meta.with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.when(
        ((pl.col("ohanism_side") == "BUY")  & (pl.col("outcome_side") == "Up"))
        | ((pl.col("ohanism_side") == "SELL") & (pl.col("outcome_side") == "Down"))
    )
    .then(pl.lit(1))
    .otherwise(pl.lit(-1))
    .alias("canonical_sign"),
])

# Compute Up-equivalent price for every fill
# If outcome_side=Up, Up_price = price
# If outcome_side=Down, Up_price = 1 - price
has_meta = has_meta.with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("up_price")
)

# Rebate-favored = quotes on the LOWER-priced token
# If up_price > 0.5: Down is OTM → rebate-favored = Down
# If up_price < 0.5: Up is OTM → rebate-favored = Up
# ohanism's quoted side is the token in outcome_side
# (they're quoting ASK on that token = SELL that token)
has_meta = has_meta.with_columns(
    pl.when(pl.col("up_price") > 0.5)
    .then(pl.lit("Down"))
    .when(pl.col("up_price") < 0.5)
    .then(pl.lit("Up"))
    .otherwise(pl.lit("ATM"))
    .alias("rebate_favored_token"),
    pl.when(pl.col("ohanism_side") == "SELL")
    .then(pl.col("outcome_side"))  # SELL of outcome_side token
    .when(pl.col("ohanism_side") == "BUY")
    .then(
        pl.when(pl.col("outcome_side") == "Up").then(pl.lit("Down")).otherwise(pl.lit("Up"))
    )  # BUY of outcome_side = quoting SELL on the other side
    .otherwise(pl.lit("UNKNOWN"))
    .alias("ohanism_quoted_token"),
)

# Fix: BUY means ohanism received tokens (they were the resting BUY order)
# Their "quoted side" = the token they wanted to BUY = outcome_side
# So for rebate check: ohanism's quoted token = outcome_side (for both BUY and SELL)
# But rebate is on MAKER's fill. A SELL order = ASK resting, SELL Down → Down is the quoted token.
# A BUY order = BID resting, BUY Down → Down is the quoted token.
# In either case: quoted token = outcome_side.
has_meta = has_meta.with_columns(
    pl.col("outcome_side").alias("ohanism_quoted_token")
)

has_meta = has_meta.with_columns(
    (pl.col("ohanism_quoted_token") == pl.col("rebate_favored_token")).alias("rebate_aligned")
)

# ── Section 1: Up-price distribution by asset ──────────────────────────────
print("\n=== UP-PRICE DISTRIBUTION BY ASSET (p50 / fraction > 0.5) ===")
for asset in ["BTC", "ETH", "SOL", "XRP"]:
    sub = has_meta.filter(pl.col("asset_symbol") == asset)
    up_p = sub["up_price"].to_numpy()
    frac_above = (up_p > 0.5).mean()
    print(f"  {asset}: p50={np.median(up_p):.3f}  frac(Up>0.5)={frac_above:.3f}  n={len(sub)}")

# ── Section 2: Rebate alignment by asset ──────────────────────────────────
print("\n=== REBATE ALIGNMENT BY ASSET ===")
for asset in ["BTC", "ETH", "SOL", "XRP"]:
    sub = has_meta.filter(pl.col("asset_symbol") == asset)
    aligned = sub.filter(pl.col("rebate_aligned") == True).height
    total = len(sub)
    pct = aligned / total * 100 if total > 0 else 0
    print(f"  {asset}: aligned={aligned}/{total} ({pct:.1f}%)")

# ── Section 3: XRP 5m canonical bias controlled for rebate ─────────────────
print("\n=== XRP 5m: BIAS BY ATM STATE ===")
xrp5m = has_meta.filter(
    (pl.col("asset_symbol") == "XRP") & (pl.col("horizon") == "5m")
)
for atm_state in ["Up>0.5 (Down rebate-favored)", "Up<0.5 (Up rebate-favored)"]:
    if "Up>0.5" in atm_state:
        sub = xrp5m.filter(pl.col("up_price") > 0.5)
    else:
        sub = xrp5m.filter(pl.col("up_price") < 0.5)
    n = len(sub)
    if n == 0:
        continue
    net_lu = sub.filter(pl.col("canonical_sign") == 1)["price_f"].sum()
    total_n = sub["price_f"].sum()
    canon_long = sub.filter(pl.col("canonical_sign") == 1).height
    print(f"  {atm_state}: n={n}, long-Up fills={canon_long} ({canon_long/n*100:.1f}%)")

# ── Section 4: Verdict ──────────────────────────────────────────────────────
xrp5m_all = has_meta.filter(
    (pl.col("asset_symbol") == "XRP") & (pl.col("horizon") == "5m")
)
xrp_up_gt_50 = xrp5m_all.filter(pl.col("up_price") > 0.5).height / len(xrp5m_all) * 100

print(f"\n=== VERDICT ===")
print(f"XRP 5m fills where Up-price > 0.5 (Down rebate-favored): {xrp_up_gt_50:.1f}%")
overall_aligned = has_meta.filter(pl.col("rebate_aligned")).height / len(has_meta) * 100
print(f"Overall rebate-alignment rate: {overall_aligned:.1f}%")

if xrp_up_gt_50 > 70:
    print("FINDING: XRP Up is disproportionately > 0.5 in this window.")
    print("  The 31.7% long-Up XRP skew is MECHANICAL (rebate, not alpha).")
elif xrp_up_gt_50 > 50:
    print("FINDING: XRP Up slightly > 0.5 on average. Bias partially mechanical.")
    print("  XRP-specific alpha cannot be ruled out — investigate in Phase 5.")
else:
    print("FINDING: XRP Up symmetric or OTM-dominant. Bias NOT fully mechanical.")
    print("  XRP 31.7% long-Up skew = real alpha overlay. Per-asset sigma in Phase 4.")
