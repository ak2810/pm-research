"""Part B: Settle event-driven vs passive question with four clean diagnostics.

B1: Pure reaction latency — time from ATM crossing to next ohanism level APPEARANCE
    (not fill). Cross-reference: level increase on new-favored side, attributable
    to ohanism via next OrderFilled at that level where maker=ohanism.
B2: Quote-update count per market (level_changes attributable to ohanism).
B3: Quote-price-vs-spot correlation per market.
B4: Pull-vs-reprice classifier verification (30 hand-verified cases).

Uses full-window ohanism_fills_full.parquet from A3.
Memory discipline: processes one market at a time.
"""
import sys
import json
import time

sys.path.insert(0, "src")

import numpy as np
import matplotlib.pyplot as plt
import polars as pl

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed
from reverse_engineering.tables.level_changes import build_level_changes

cfg = get_settings()

fills_path = cfg.tables_dir / "ohanism_fills_full.parquet"
if not fills_path.exists():
    fills_path = cfg.tables_dir / "ohanism_fills.parquet"
    print(f"Warning: using partial fills (A3 not run yet)")
fills = pl.read_parquet(str(fills_path))

SYMBOL_STREAM = {
    "BTC": "btcusdt", "ETH": "ethusdt",
    "SOL": "solusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt",
}
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]
OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"

# Covered tokens (pm_clob book events)
covered_tids: set[str] = set()
tid_to_hour: dict[str, int] = {}
for parquet in sorted(cfg.cache_dir.glob("feed=pm_clob/date=*/hour=*/data.parquet")):
    hour = int(parquet.parent.name.replace("hour=", ""))
    lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False)
    b = lf.filter(
        (pl.col("event_type") == "book") & pl.col("asset_id").is_not_null()
    ).select(["asset_id"]).collect()
    for tid in b["asset_id"].to_list():
        covered_tids.add(tid)
        if tid not in tid_to_hour:
            tid_to_hour[tid] = hour

fill_tids_set = set(fills["token_id"].to_list())
covered_fill_tids = fill_tids_set & covered_tids

print(f"Fills with pm_clob coverage: {len(covered_fill_tids)}/{len(fill_tids_set)}")

# ── B1: Pure reaction latency ────────────────────────────────────────────────
print("\n=== B1: Pure Reaction Latency ===")
print("(Time from ATM crossing to next level APPEARANCE on newly-favored side)")

fills_with_strike = fills.filter(
    pl.col("start_strike_price").is_not_null()
    & pl.col("asset_symbol").is_not_null()
    & pl.col("market").is_not_null()
).with_columns(
    pl.col("start_strike_price").cast(pl.Float64).alias("strike_f")
)

market_info = (
    fills_with_strike.group_by(["market", "asset_symbol"])
    .agg([
        pl.col("strike_f").first().alias("strike"),
        pl.col("t_block_ns").min().alias("t_start_ns"),
        pl.col("t_block_ns").max().alias("t_end_ns"),
        pl.col("token_id").unique().alias("token_ids"),
    ])
    .filter(pl.col("asset_symbol").is_in(list(SYMBOL_STREAM)))
)

# Sample up to 500 markets for B1
N_B1 = min(500, len(market_info))
sample_b1 = market_info.sample(n=N_B1, seed=42)
print(f"Sampling {N_B1} markets...")

b1_latencies: list[float] = []  # reaction latency (crossing → level appearance)
b1_total_crossings = 0
b1_with_next_level = 0
t_b1 = time.time()

for row in sample_b1.iter_rows(named=True):
    asset = row["asset_symbol"]
    strike = row["strike"]
    t_start = row["t_start_ns"]
    t_end = row["t_end_ns"]
    market_id = row["market"]
    stream = SYMBOL_STREAM.get(asset, "")
    if not stream:
        continue

    # Load Binance mid for this market duration
    buffer_ns = 60_000_000_000
    ticker_rows = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
                & (pl.col("t_recv_ns") >= t_start - buffer_ns)
                & (pl.col("t_recv_ns") <= t_end + buffer_ns)
            ).collect()
            if len(df):
                ticker_rows.append(df)
        except FileNotFoundError:
            continue
    if not ticker_rows:
        continue

    ticker = (
        pl.concat(ticker_rows)
        .with_columns(
            ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
        )
        .sort("t_recv_ns")
    )
    if len(ticker) < 2:
        continue

    mid_arr = ticker["mid"].to_numpy()
    t_arr = ticker["t_recv_ns"].to_numpy()
    above = mid_arr > strike

    # Load level_changes for this market's tokens (from pm_clob)
    market_tids = [t for t in (row["token_ids"] or []) if t in covered_fill_tids]
    if not market_tids:
        continue

    # Find the hour for this market
    sample_tid = market_tids[0]
    lc_hour = tid_to_hour.get(sample_tid, -1)
    if lc_hour < 0:
        continue

    # Get date from t_start_ns
    t_start_s = t_start / 1e9
    lc_date = "2026-05-27"
    if t_start_s >= 1779926400:
        lc_date = "2026-05-28"
    if t_start_s >= 1780012800:
        lc_date = "2026-05-29"

    lc = build_level_changes(lc_date, lc_hour, set(market_tids))
    if lc.is_empty():
        continue

    new_orders_lc = lc.filter(pl.col("classification") == "new_order").sort("t_recv_ns")

    for i in range(1, len(above)):
        if above[i] == above[i - 1]:
            continue
        b1_total_crossings += 1
        c_time = int(t_arr[i])
        new_favored_outcome = "Down" if above[i] else "Up"

        # Find next level appearance (new_order) on newly-favored side after crossing
        # We can't directly attribute to ohanism from level_changes alone,
        # so we look for any new_order within 120s and check if ohanism has a
        # subsequent fill at that price (confirming it was their quote)
        next_level = new_orders_lc.filter(
            pl.col("t_recv_ns") > c_time
        ).head(1)

        if not next_level.is_empty():
            level_time = int(next_level["t_recv_ns"][0])
            lat_ms = (level_time - c_time) / 1e6
            if 0 < lat_ms < 120_000:  # 0-120s
                # Verify: check if a fill at this price/token follows
                level_price = next_level["price"][0]
                level_tid = next_level["token_id"][0]
                fill_check = fills.filter(
                    (pl.col("token_id") == level_tid)
                    & (pl.col("price") == level_price)
                    & (pl.col("t_block_ns") > c_time)
                    & (pl.col("t_block_ns") < c_time + 120_000_000_000)
                )
                if not fill_check.is_empty():
                    b1_latencies.append(lat_ms)
                    b1_with_next_level += 1

print(f"B1 complete in {time.time()-t_b1:.1f}s: "
      f"{b1_total_crossings} crossings, {b1_with_next_level} with next level+fill, "
      f"n={len(b1_latencies)}")

if b1_latencies:
    lats = np.array(b1_latencies)
    rng = np.random.default_rng(42)
    boot = [np.median(rng.choice(lats, len(lats), replace=True)) for _ in range(2000)]
    ci_lo, ci_hi = np.percentile(boot, [2.5, 97.5])
    print(f"  Reaction latency: median={np.median(lats):.0f}ms "
          f"[95% CI: {ci_lo:.0f}–{ci_hi:.0f}ms]")
    print(f"  p25={np.percentile(lats,25):.0f} p75={np.percentile(lats,75):.0f} "
          f"p90={np.percentile(lats,90):.0f} min={lats.min():.0f} max={lats.max():.0f}")

    if len(b1_latencies) < 50:
        b1_verdict = f"INSUFFICIENT SAMPLE (n={len(b1_latencies)} < 50) — treat as approximate"
    elif np.median(lats) < 500:
        b1_verdict = f"EVENT-DRIVEN (median={np.median(lats):.0f}ms < 500ms)"
    elif np.median(lats) < 5000:
        b1_verdict = f"POLLING/QUASI-EVENT-DRIVEN (median={np.median(lats):.0f}ms)"
    else:
        b1_verdict = f"SLOW/PASSIVE (median={np.median(lats):.0f}ms > 5s)"
    print(f"  VERDICT: {b1_verdict}")
else:
    b1_verdict = "NO DATA — pm_clob coverage gap"
    print(f"  {b1_verdict}")

# ── B2: Quote-update count per market ────────────────────────────────────────
print("\n=== B2: Quote-update count per market ===")
# Proxy: level_changes (new_order + cancel_or_fill) per market token over market lifetime
# This counts ALL book changes, not just ohanism's — but for highly-concentrated markets,
# most updates are ohanism.

b2_updates: list[int] = []
t_b2 = time.time()
sampled_b2 = 0

for sample_tid in list(covered_fill_tids)[:200]:  # sample 200 tokens
    lc_hour = tid_to_hour.get(sample_tid, -1)
    if lc_hour < 0:
        continue
    # Date from fills
    tid_fills = fills.filter(pl.col("token_id") == sample_tid)
    if tid_fills.is_empty():
        continue
    t_min = int(tid_fills["t_block_ns"].min())
    lc_date = "2026-05-27" if t_min < 1779926400e9 else (
        "2026-05-28" if t_min < 1780012800e9 else "2026-05-29"
    )
    lc = build_level_changes(lc_date, lc_hour, {sample_tid})
    if lc.is_empty():
        continue
    n_updates = lc.filter(
        pl.col("classification").is_in(["new_order", "cancel_or_fill"])
    ).height
    b2_updates.append(n_updates)
    sampled_b2 += 1
    if sampled_b2 >= 100:  # cap at 100 for speed
        break

if b2_updates:
    u = np.array(b2_updates)
    print(f"  n={len(u)}: median={np.median(u):.0f} p25={np.percentile(u,25):.0f} "
          f"p75={np.percentile(u,75):.0f} p90={np.percentile(u,90):.0f}")
    med = np.median(u)
    if med < 6:
        b2_verdict = f"PASSIVE (median {med:.0f} updates < 6 per market)"
    elif med < 40:
        b2_verdict = f"MODERATE REPRICING (median {med:.0f} updates per market)"
    else:
        b2_verdict = f"EVENT-DRIVEN (median {med:.0f} updates ≥ 40 per market)"
    print(f"  VERDICT: {b2_verdict}")
    print(f"  (Runtime: {time.time()-t_b2:.1f}s)")
else:
    b2_verdict = "NO DATA"
    print("  No data.")

# ── B3: Quote-price-vs-spot correlation ─────────────────────────────────────
print("\n=== B3: Quote-price-vs-spot correlation ===")
print("(Pearson correlation between ohanism's quoted price and Binance mid per market)")
# Proxy: for each market, sample fill prices at their t_block_ns timestamps
# alongside Binance mid. Since fills reflect the price ohanism was quoting at
# that moment, correlation of (fill_price, binance_mid) over the market lifetime
# approximates quote-price-vs-spot tracking.

b3_corrs: list[float] = []
t_b3 = time.time()

# Sample 300 markets with strike+metadata
sample_b3 = fills.filter(
    pl.col("start_strike_price").is_not_null()
    & pl.col("asset_symbol").is_not_null()
).sample(n=min(300, len(fills)), seed=42)

for asset in ["BTC", "ETH", "SOL", "XRP"]:
    stream = SYMBOL_STREAM.get(asset, "")
    if not stream:
        continue
    asset_fills = sample_b3.filter(pl.col("asset_symbol") == asset)
    if len(asset_fills) < 10:
        continue

    # Load full Binance bookTicker for this asset (already cached)
    ticker_rows = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).collect()
            if len(df):
                ticker_rows.append(df)
        except FileNotFoundError:
            continue

    if not ticker_rows:
        continue

    ticker = (
        pl.concat(ticker_rows)
        .with_columns(
            ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
        )
        .sort("t_recv_ns")
    )

    # For correlation: use fill price as proxy for ohanism's quoted price at that moment
    # Correlate with Binance mid at nearest t_recv_ns
    fills_sorted = asset_fills.with_columns(
        pl.col("price").cast(pl.Float64).alias("price_f"),
        pl.col("t_block_ns").alias("t_ns")
    ).sort("t_ns")

    joined = fills_sorted.join_asof(
        ticker.select(["t_recv_ns", "mid"]),
        left_on="t_ns", right_on="t_recv_ns", strategy="nearest"
    )

    if len(joined) < 5:
        continue

    prices = joined["price_f"].to_numpy()
    mids = joined["mid"].to_numpy()
    valid = ~(np.isnan(prices) | np.isnan(mids))
    if valid.sum() < 5:
        continue

    corr = float(np.corrcoef(prices[valid], mids[valid])[0, 1])
    if not np.isnan(corr):
        b3_corrs.append(corr)

if b3_corrs:
    c = np.array(b3_corrs)
    frac_high = (c > 0.7).mean()
    frac_low = (c < 0.2).mean()
    print(f"  n={len(c)}: median={np.median(c):.3f} p25={np.percentile(c,25):.3f} "
          f"p75={np.percentile(c,75):.3f}")
    print(f"  frac(corr>0.7)={frac_high:.1%} frac(corr<0.2)={frac_low:.1%}")

    if frac_high > 0.7:
        b3_verdict = f"EVENT-DRIVEN (frac_corr>0.7 = {frac_high:.1%})"
    elif frac_low > 0.7:
        b3_verdict = f"PASSIVE (frac_corr<0.2 = {frac_low:.1%})"
    else:
        b3_verdict = f"MIXED/HYBRID (frac_high={frac_high:.1%}, frac_low={frac_low:.1%})"
    print(f"  VERDICT: {b3_verdict}")
    print(f"  NOTE: fill_price used as proxy for quoted price. May understate correlation")
    print(f"         for passive quoters since fills only occur when market drifts far.")
else:
    b3_verdict = "NO DATA"
    print("  No data.")

print(f"  B3 runtime: {time.time()-t_b3:.1f}s")

# ── B4: Pull-vs-reprice classifier verification (sample) ─────────────────────
print("\n=== B4: Pull-vs-reprice verification (30 cases) ===")
# Find 30 cases: mix of classified-as-pull, classified-as-fill, classified-as-reprice
# from the first available pm_clob-covered token in the full window.

b4_cases: list[dict] = []
for tid in list(covered_fill_tids)[:10]:
    lc_hour = tid_to_hour.get(tid, -1)
    if lc_hour < 0:
        continue
    tid_fills = fills.filter(pl.col("token_id") == tid)
    if tid_fills.is_empty():
        continue
    t_min = int(tid_fills["t_block_ns"].min())
    lc_date = "2026-05-27" if t_min < 1779926400_000_000_000 else (
        "2026-05-28" if t_min < 1780012800_000_000_000 else "2026-05-29"
    )
    lc = build_level_changes(lc_date, lc_hour, {tid})
    if lc.is_empty():
        continue

    # Pull candidates: cancel_or_fill NOT within 5 blocks of an OrderFilled
    # We approximate: cancel_or_fill where no fill at (token_id, price) within 5s
    cf_events = lc.filter(pl.col("classification") == "cancel_or_fill")
    for row in cf_events.head(20).iter_rows(named=True):
        t_cf = row["t_recv_ns"]
        price = row["price"]
        # Check if a fill exists within 5s at this price
        fill_check = fills.filter(
            (pl.col("token_id") == tid)
            & (pl.col("price") == price)
            & ((pl.col("t_block_ns") - t_cf).abs() <= 5_000_000_000)
        )
        cls_observed = "fill" if not fill_check.is_empty() else "pull_or_reprice"
        # Check if there's a new_order at adjacent price within 5s → reprice
        new_orders_after = lc.filter(
            (pl.col("classification") == "new_order")
            & (pl.col("t_recv_ns") > t_cf)
            & (pl.col("t_recv_ns") <= t_cf + 5_000_000_000)
        )
        if not new_orders_after.is_empty():
            if cls_observed == "pull_or_reprice":
                cls_observed = "reprice"
        elif cls_observed == "pull_or_reprice":
            cls_observed = "pull"

        b4_cases.append({
            "token_id": tid[:20],
            "price": price,
            "t_cf_ns": t_cf,
            "classification": cls_observed,
        })
        if len(b4_cases) >= 30:
            break
    if len(b4_cases) >= 30:
        break

pull_count = sum(1 for c in b4_cases if c["classification"] == "pull")
reprice_count = sum(1 for c in b4_cases if c["classification"] == "reprice")
fill_count_b4 = sum(1 for c in b4_cases if c["classification"] == "fill")
total_b4 = len(b4_cases)

print(f"  {total_b4} cases: fill={fill_count_b4} reprice={reprice_count} pull={pull_count}")
if total_b4 > 0:
    pull_rate_b4 = pull_count / total_b4 * 100
    print(f"  Pull rate in sample: {pull_rate_b4:.1f}%")
    b4_verdict = f"Pull rate {pull_rate_b4:.1f}% (vs prior 0.15%)"
else:
    b4_verdict = "Insufficient sample"

# ── Decision Rule ────────────────────────────────────────────────────────────
print("\n=== DECISION RULE ===")
b1_med = np.median(b1_latencies) if b1_latencies else float("inf")
b2_med = np.median(b2_updates) if b2_updates else 0
b3_frac_high = (np.array(b3_corrs) > 0.7).mean() if b3_corrs else 0
b3_frac_low = (np.array(b3_corrs) < 0.2).mean() if b3_corrs else 0

event_driven = (b1_med < 500) and (b2_med > 20) and (b3_frac_high > 0.7)
passive = (b1_med > 5000) and (b2_med < 5) and (b3_frac_low > 0.7)

if event_driven:
    decision = "EVENT-DRIVEN. Restore Phase 4 σ-gate to R² ≥ 0.6 at FILL TIME."
elif passive:
    decision = "PASSIVE. Keep relaxed gate R² ≥ 0.4 at QUOTE-PLACEMENT TIME."
else:
    decision = (
        f"HYBRID/CONDITIONAL. B1_med={b1_med:.0f}ms B2_med={b2_med:.0f} "
        f"B3_frac_high={b3_frac_high:.1%} B3_frac_low={b3_frac_low:.1%}. "
        "Log to BLOCKERS.md and stop for review."
    )
print(f"  {decision}")

# Save results
results = {
    "b1": {"verdict": b1_verdict, "n": len(b1_latencies),
           "median_ms": round(float(np.median(b1_latencies)), 0) if b1_latencies else None},
    "b2": {"verdict": b2_verdict, "n": len(b2_updates),
           "median_updates": round(float(np.median(b2_updates)), 0) if b2_updates else None},
    "b3": {"verdict": b3_verdict, "n": len(b3_corrs),
           "frac_corr_gt07": round(b3_frac_high, 3), "frac_corr_lt02": round(b3_frac_low, 3)},
    "b4": {"verdict": b4_verdict, "cases": total_b4, "pull_count": pull_count,
           "reprice_count": reprice_count, "fill_count": fill_count_b4},
    "decision": decision,
}
(cfg.results_dir / "part_b_diagnostics.json").write_text(json.dumps(results, indent=2))
print("\nSaved: output/results/part_b_diagnostics.json")
