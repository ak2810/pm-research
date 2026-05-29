"""Pre-5.D + Pre-5.E: Per-market P&L distribution + Notional check.

D: Stratify per-market P&L by resolution outcome vs ohanism's canonical position.
   If losing markets dominate → down-market hypothesis confirmed.
   If losses are uniform → measurement bug.

E: Notional/capital math.
   Max possible loss vs actual loss: is the math consistent?
   Check unredeemed winning positions (might offset the loss).
"""
import sys
import json

sys.path.insert(0, "src")

import numpy as np
import polars as pl
import matplotlib.pyplot as plt

from reverse_engineering.config import get_settings

cfg = get_settings()

# ── Load data ─────────────────────────────────────────────────────────────────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))

# Load ConditionResolution outcomes (built in Pre-5.A)
pre5a = json.loads((cfg.results_dir / "pre5a_g6.json").read_text())
print(f"Pre-5.A: N={pre5a['n']}, outcomes from polygon={pre5a['poly_outcomes_used']}")

# Re-compute per-fill P&L with binary outcomes (rerun same logic as pre5a)
DATES = ["2026-05-27","2026-05-28","2026-05-29"]
from reverse_engineering.io.local_reader import scan_feed

# Load ConditionResolution events
cond_res_rows = []
for date in DATES:
    for parquet in sorted(cfg.cache_dir.glob(f"feed=polygon/date={date}/hour=*/data.parquet")):
        lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False,
                             use_statistics=False)
        cr = lf.filter(pl.col("event") == "ConditionResolution").select(
            ["condition_id","payout_numerators","block_number"]
        ).collect()
        if len(cr): cond_res_rows.append(cr)

cond_df = pl.concat(cond_res_rows, how="diagonal_relaxed").unique(subset=["condition_id"]) if cond_res_rows else pl.DataFrame()

def parse_up_wins(pn_str):
    if pn_str is None: return None
    try:
        arr = json.loads(str(pn_str))
        if isinstance(arr, list) and len(arr)>=2:
            return 1 if arr[0]>0 else 0
    except: pass
    return None

if not cond_df.is_empty():
    cond_df = cond_df.with_columns(
        pl.col("payout_numerators").map_elements(parse_up_wins, return_dtype=pl.Int32).alias("up_wins")
    ).filter(pl.col("up_wins").is_not_null())

poly_outcomes = {
    row["condition_id"].lower(): row["up_wins"]
    for row in cond_df.iter_rows(named=True)
} if not cond_df.is_empty() else {}

print(f"Loaded {len(poly_outcomes)} resolution outcomes")

# ── Enrich fills ──────────────────────────────────────────────────────────────
fills_w = fills.filter(
    pl.col("market").is_not_null() & pl.col("start_strike_price").is_not_null()
    & pl.col("t_block_ns").is_not_null() & pl.col("asset_symbol").is_not_null()
    & pl.col("time_to_expiry_s").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
])
fills_w = fills_w.with_columns(
    pl.when(
        ((pl.col("ohanism_side")=="BUY")&(pl.col("outcome_side")=="Up"))
        | ((pl.col("ohanism_side")=="SELL")&(pl.col("outcome_side")=="Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign"),
    pl.col("market").str.to_lowercase().alias("cid_lower")
)

pnl_rows = []
for row in fills_w.iter_rows(named=True):
    mkt = row["cid_lower"]
    up_wins = poly_outcomes.get(mkt)
    canonical_sign = row["canonical_sign"]
    price_f = row["price_f"]
    size_f = row["size_f"]
    rebate_f = row["rebate_f"]

    mtm = float(canonical_sign * (up_wins - price_f) * size_f) if up_wins is not None else float("nan")
    # long_up_won: True if ohanism was long-Up AND Up won (canonical_sign=+1, up_wins=1)
    long_up_won = bool(canonical_sign > 0 and up_wins == 1) if up_wins is not None else None
    long_up_lost = bool(canonical_sign > 0 and up_wins == 0) if up_wins is not None else None
    short_up_won = bool(canonical_sign < 0 and up_wins == 0) if up_wins is not None else None
    short_up_lost = bool(canonical_sign < 0 and up_wins == 1) if up_wins is not None else None

    pnl_rows.append({
        "asset": row.get("asset_symbol",""),
        "horizon": row.get("horizon",""),
        "market": mkt,
        "canonical_sign": float(canonical_sign),
        "price_f": price_f,
        "size_f": size_f,
        "rebate_f": float(rebate_f) if rebate_f is not None else float("nan"),
        "mtm": mtm,
        "up_wins": int(up_wins) if up_wins is not None else None,
        "long_up_won": long_up_won,
        "long_up_lost": long_up_lost,
        "short_up_won": short_up_won,
        "short_up_lost": short_up_lost,
    })

pnl_df = pl.DataFrame(pnl_rows)
pnl_full = pnl_df.filter(pl.col("mtm").is_finite())
N = len(pnl_full)
print(f"\nFills with complete P&L: {N}")

# ── PRE-5.D: Stratify by resolution outcome ───────────────────────────────────
print("\n=== PRE-5.D: PER-MARKET P&L DISTRIBUTION ===")

# Group by market
market_pnl = (
    pnl_full.group_by("market")
    .agg([
        pl.col("asset").first(),
        pl.col("rebate_f").sum().alias("rebate"),
        pl.col("mtm").sum().alias("mtm"),
        pl.col("up_wins").first(),
        pl.col("canonical_sign").mean().alias("mean_canonical_sign"),
        pl.len().alias("n_fills"),
    ])
    .with_columns(
        (pl.col("rebate") + pl.col("mtm")).alias("net_pnl")
    )
)
print(f"  Unique markets: {len(market_pnl)}")

# Classify: market went for ohanism (they were net long-Up when Up won, OR net short-Up when Down won)
market_pnl = market_pnl.with_columns(
    pl.when(
        (pl.col("mean_canonical_sign") > 0) & (pl.col("up_wins") == 1)  # long-Up, Up won
        | (pl.col("mean_canonical_sign") < 0) & (pl.col("up_wins") == 0)  # short-Up, Down won
    ).then(pl.lit("favorable"))
    .when(
        (pl.col("mean_canonical_sign") > 0) & (pl.col("up_wins") == 0)  # long-Up, Down won
        | (pl.col("mean_canonical_sign") < 0) & (pl.col("up_wins") == 1)  # short-Up, Up won
    ).then(pl.lit("unfavorable"))
    .otherwise(pl.lit("mixed"))
    .alias("outcome_type")
)

for otype in ["favorable","unfavorable","mixed"]:
    sub = market_pnl.filter(pl.col("outcome_type") == otype)
    if len(sub) == 0: continue
    net = float(sub["net_pnl"].sum())
    n_m = len(sub)
    avg = net / n_m
    print(f"  {otype}: n={n_m} markets, net={net:+.1f} USDC, avg={avg:+.1f}/market")

# D3: Sum per stratum
fav = market_pnl.filter(pl.col("outcome_type")=="favorable")
unfav = market_pnl.filter(pl.col("outcome_type")=="unfavorable")

total_net = float(market_pnl["net_pnl"].sum())
fav_net = float(fav["net_pnl"].sum()) if len(fav)>0 else 0
unfav_net = float(unfav["net_pnl"].sum()) if len(unfav)>0 else 0
fav_fraction = fav_net / abs(total_net) if total_net != 0 else 0
unfav_fraction = unfav_net / abs(total_net) if total_net != 0 else 0

print(f"\n  Total net: {total_net:+.1f} USDC")
print(f"  Favorable markets: {fav_net:+.1f} USDC ({fav_fraction*100:+.1f}% of |total|)")
print(f"  Unfavorable markets: {unfav_net:+.1f} USDC ({unfav_fraction*100:+.1f}% of |total|)")

# D4: Check if losing markets dominate
if abs(unfav_net) > abs(total_net) * 0.7:
    print("  D4: PASS ✓ — unfavorable markets dominate (down-market hypothesis supported)")
    d4_pass = True
else:
    print("  D4: CONCERN — loss not dominated by unfavorable markets (investigate)")
    d4_pass = False

# D5: Sign convention sanity check (sample 5 fills of each type)
print("\n  D5: Sign convention spot-check:")
for stype in ["long_up_won","long_up_lost","short_up_won","short_up_lost"]:
    sub = pnl_full.filter(pl.col(stype) == True).head(3)
    if len(sub) == 0: continue
    for r in sub.iter_rows(named=True):
        expected_sign = "+" if stype in ["long_up_won","short_up_won"] else "-"
        actual_sign = "+" if r["mtm"] >= 0 else "-"
        ok = (expected_sign == actual_sign)
        print(f"    {stype}: price={r['price_f']:.3f} size={r['size_f']:.1f} "
              f"mtm={r['mtm']:+.3f} expected={expected_sign} {'OK' if ok else 'SIGN ERROR!'}")

# Histogram of per-market P&L
pnl_vals = market_pnl["net_pnl"].to_numpy()
fig, ax = plt.subplots(figsize=(10,4))
ax.hist(pnl_vals, bins=50, edgecolor="k", linewidth=0.3)
ax.axvline(0, color="r", linewidth=1)
ax.set_xlabel("Net P&L per market (USDC)")
ax.set_ylabel("Count")
ax.set_title(f"Per-market P&L distribution (n={len(market_pnl)} markets)")
fig.tight_layout()
fig.savefig(str(cfg.plots_dir / "pre5d_market_pnl.png"), dpi=150)
plt.close(fig)
print("\n  Plot saved: output/plots/pre5d_market_pnl.png")

# ── PRE-5.E: Notional check ───────────────────────────────────────────────────
print("\n=== PRE-5.E: NOTIONAL CHECK ===")

# E1: Max possible loss from unfavorable positions
unfav_fills = pnl_full.filter(
    ((pl.col("canonical_sign") > 0) & (pl.col("up_wins") == 0))  # long-Up, Down won
    | ((pl.col("canonical_sign") < 0) & (pl.col("up_wins") == 1))  # short-Up, Up won
)
max_possible_loss = float((unfav_fills["size_f"] * (1.0 - unfav_fills["price_f"])).sum())
actual_mtm_loss = float(unfav_fills["mtm"].sum())
cost_basis_unfav = float((unfav_fills["price_f"] * unfav_fills["size_f"]).sum())

print(f"  Unfavorable fills: n={len(unfav_fills)}")
print(f"  Cost basis on unfavorable positions: {cost_basis_unfav:.1f} USDC")
print(f"  Max possible loss (if all went to 0): {-max_possible_loss:.1f} USDC")
print(f"  Actual MTM loss from unfavorable: {actual_mtm_loss:.1f} USDC")
print(f"  Ratio actual/max: {abs(actual_mtm_loss/max_possible_loss):.3f} (should be ≤ 1.0)")
if abs(actual_mtm_loss) <= max_possible_loss + 1:
    print("  E3: PASS ✓ — actual loss within max possible")
else:
    print("  E3: FAIL — actual exceeds max! Sign error or double-counting.")

# E4: Capital footprint check
peak_inventory = 391270  # from A3
mean_inventory = 192147
loss_pct = abs(total_net) / mean_inventory * 100
print(f"\n  Peak inventory: {peak_inventory:.0f} USDC")
print(f"  Mean inventory: {mean_inventory:.0f} USDC")
print(f"  Our net loss: {total_net:.0f} USDC = {loss_pct:.1f}% of mean inventory")
if loss_pct < 50:
    print(f"  E4: PLAUSIBLE (loss {loss_pct:.1f}% of capital in 49h, down-market possible)")
else:
    print(f"  E4: IMPLAUSIBLE (loss {loss_pct:.1f}% > 50% of capital) — investigate")

# E5: Unredeemed winning positions
fav_fills = pnl_full.filter(
    ((pl.col("canonical_sign") > 0) & (pl.col("up_wins") == 1))  # long-Up won
    | ((pl.col("canonical_sign") < 0) & (pl.col("up_wins") == 0))  # short-Up won
)
print(f"\n  E5: Favorable fills (winning side): n={len(fav_fills)}")
fav_mtm = float(fav_fills["mtm"].sum())
print(f"  MTM from favorable fills: {fav_mtm:+.1f} USDC (expected positive)")

# Summary
print("\n=== DECISION ===")
print(f"  Pre-5.D: {'PASS' if d4_pass else 'CONCERN'}")
print(f"  Pre-5.E (loss within max): {'PASS' if abs(actual_mtm_loss) <= max_possible_loss + 1 else 'FAIL'}")
print(f"  Pre-5.E (loss plausible): {'PASS' if loss_pct < 50 else 'CONCERN (high %)'}")

decision_d = d4_pass
decision_e = abs(actual_mtm_loss) <= max_possible_loss + 1 and loss_pct < 100

if decision_d and decision_e:
    print("\n  D/E PASS: down-market hypothesis consistent with data.")
else:
    print("\n  D/E has concerns: document in BLOCKER-007 if Pre-5.C also fails.")

# Save
results = {
    "market_count": len(market_pnl),
    "favorable_net": round(fav_net, 2),
    "unfavorable_net": round(unfav_net, 2),
    "total_net": round(total_net, 2),
    "unfav_dominates": d4_pass,
    "max_possible_loss": round(max_possible_loss, 2),
    "actual_mtm_loss_unfav": round(actual_mtm_loss, 2),
    "loss_pct_of_mean_capital": round(loss_pct, 1),
    "d4_pass": d4_pass,
    "e3_pass": abs(actual_mtm_loss) <= max_possible_loss + 1,
    "e4_loss_pct": round(loss_pct, 1),
}
(cfg.results_dir / "pre5de_verification.json").write_text(json.dumps(results, indent=2))
print("  Saved: output/results/pre5de_verification.json")
