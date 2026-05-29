"""Part B fast diagnostics — B3 and B4 only.

B1 and B2 require full level_changes pipeline (too slow for 600s).
We use existing evidence for B1/B2:
  B1 proxy: fill-latency from quote_flip.py = 18.4s median (>5s → SLOW/PASSIVE)
  B2 proxy: Phase 3 shows 99.85% reprice, 0.15% pull — but ALL participants,
            not just ohanism. Ambiguous on its own.

B3: fill_price vs Binance mid correlation per market (fast — fills only, no level_changes)
B4: 30-case pull verification (fast — level_changes for 3 tokens only)
"""
import sys
import json
import time

sys.path.insert(0, "src")

import numpy as np
import polars as pl

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed
from reverse_engineering.tables.level_changes import build_level_changes, _normalize_price

cfg = get_settings()

fills_path = cfg.tables_dir / "ohanism_fills_full.parquet"
if not fills_path.exists():
    fills_path = cfg.tables_dir / "ohanism_fills.parquet"
fills = pl.read_parquet(str(fills_path))

SYMBOL_STREAM = {
    "BTC": "btcusdt", "ETH": "ethusdt",
    "SOL": "solusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt",
}
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]

# ── B3: Quote-price-vs-spot correlation ─────────────────────────────────────
print("=== B3: Quote-price-vs-spot correlation ===")
t_b3 = time.time()

# For each fill with metadata, compute Up-equivalent price and find nearest Binance mid
# High correlation = quotes track spot (event-driven)
# Low correlation = quotes don't track spot (passive post-once)

has_strike = fills.filter(
    pl.col("start_strike_price").is_not_null()
    & pl.col("asset_symbol").is_not_null()
    & pl.col("t_block_ns").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("start_strike_price").cast(pl.Float64).alias("strike_f"),
])

# Compute Up-equivalent price
has_strike = has_strike.with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("up_price")
)

b3_corrs: list[float] = []

for asset, stream in SYMBOL_STREAM.items():
    asset_fills = has_strike.filter(pl.col("asset_symbol") == asset)
    if len(asset_fills) < 20:
        continue

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

    # Group by market, compute correlation between up_price and (mid/strike)
    markets = asset_fills.select(["market"]).unique()["market"].to_list()

    for mkt in markets[:50]:  # cap at 50 markets per asset
        mkt_fills = asset_fills.filter(
            (pl.col("market") == mkt) & pl.col("t_block_ns").is_not_null()
        ).sort("t_block_ns")

        if len(mkt_fills) < 5:
            continue

        joined = mkt_fills.join_asof(
            ticker.select(["t_recv_ns", "mid"]),
            left_on="t_block_ns",
            right_on="t_recv_ns",
            strategy="nearest",
        )

        if joined["mid"].null_count() > len(joined) // 2:
            continue

        up_prices = joined["up_price"].to_numpy()
        mids = joined["mid"].to_numpy()
        strikes = joined["strike_f"].to_numpy()
        valid = ~(np.isnan(up_prices) | np.isnan(mids) | np.isnan(strikes) | (strikes == 0))
        if valid.sum() < 5:
            continue

        # Normalize: spot displacement = mid/strike - 1 (positive = Up winning)
        spot_disp = mids[valid] / strikes[valid] - 1
        up_p = up_prices[valid]
        corr = float(np.corrcoef(spot_disp, up_p)[0, 1])
        if not np.isnan(corr):
            b3_corrs.append(corr)

b3_n = len(b3_corrs)
if b3_corrs:
    c = np.array(b3_corrs)
    frac_high = (c > 0.7).mean()
    frac_low = (c < 0.2).mean()
    med_corr = float(np.median(c))
    print(f"  n={b3_n}: median_corr={med_corr:.3f}")
    print(f"  frac(corr>0.7)={frac_high:.1%}  frac(corr<0.2)={frac_low:.1%}")
    print(f"  Runtime: {time.time()-t_b3:.1f}s")

    if frac_high > 0.7:
        b3_verdict = f"EVENT-DRIVEN: frac(corr>0.7)={frac_high:.1%}"
    elif frac_low > 0.7:
        b3_verdict = f"PASSIVE: frac(corr<0.2)={frac_low:.1%} — fill prices NOT tracking spot"
    else:
        b3_verdict = f"MIXED: frac_high={frac_high:.1%} frac_low={frac_low:.1%} median_corr={med_corr:.3f}"
    print(f"  B3 VERDICT: {b3_verdict}")
else:
    b3_verdict = "NO DATA"
    print("  No data.")

# ── B4: Pull-vs-reprice verification ─────────────────────────────────────────
print("\n=== B4: Pull-vs-reprice verification (30 cases) ===")
t_b4 = time.time()

covered_tids: set[str] = set()
tid_to_hour: dict[str, int] = {}
for parquet in sorted(cfg.cache_dir.glob("feed=pm_clob/date=*/hour=*/data.parquet")):
    hour = int(parquet.parent.name.replace("hour=", ""))
    lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False,
                          use_statistics=False)
    b = lf.filter(
        (pl.col("event_type") == "book") & pl.col("asset_id").is_not_null()
    ).select(["asset_id"]).collect()
    for tid in b["asset_id"].to_list():
        covered_tids.add(tid)
        if tid not in tid_to_hour:
            tid_to_hour[tid] = hour

fill_tids = set(fills["token_id"].to_list())
covered_fill_tids = fill_tids & covered_tids

b4_cases: list[dict] = []
tids_tried = 0

for tid in list(covered_fill_tids):
    if len(b4_cases) >= 30:
        break
    tids_tried += 1
    if tids_tried > 20:
        break

    lc_hour = tid_to_hour.get(tid, -1)
    if lc_hour < 0:
        continue
    tid_fills = fills.filter(pl.col("token_id") == tid)
    if tid_fills.is_empty():
        continue
    t_min = int(tid_fills["t_block_ns"].min())
    if t_min < 1779926400_000_000_000:
        lc_date = "2026-05-27"
    elif t_min < 1780012800_000_000_000:
        lc_date = "2026-05-28"
    else:
        lc_date = "2026-05-29"

    lc = build_level_changes(lc_date, lc_hour, {tid})
    if lc.is_empty():
        continue

    cf_events = lc.filter(pl.col("classification") == "cancel_or_fill")

    for row in cf_events.head(15).iter_rows(named=True):
        if len(b4_cases) >= 30:
            break
        t_cf = row["t_recv_ns"]
        price = row["price"]

        fill_check = fills.filter(
            (pl.col("token_id") == tid)
            & (pl.col("price") == _normalize_price(price))
            & ((pl.col("t_block_ns") - t_cf).abs() <= 5_000_000_000)
        )
        if not fill_check.is_empty():
            cls = "fill"
        else:
            new_after = lc.filter(
                (pl.col("classification") == "new_order")
                & (pl.col("t_recv_ns") > t_cf)
                & (pl.col("t_recv_ns") <= t_cf + 5_000_000_000)
            )
            cls = "reprice" if not new_after.is_empty() else "pull"

        b4_cases.append({"cls": cls, "token_id": tid[:16], "price": price})

if b4_cases:
    counts = {}
    for c in b4_cases:
        counts[c["cls"]] = counts.get(c["cls"], 0) + 1
    total_b4 = len(b4_cases)
    pull_rate = counts.get("pull", 0) / total_b4 * 100
    print(f"  {total_b4} cases: {counts}")
    print(f"  Pull rate: {pull_rate:.1f}%  (prior: 0.15%)")
    print(f"  Runtime: {time.time()-t_b4:.1f}s")
    b4_verdict = f"Pull rate {pull_rate:.1f}% (n={total_b4})"
else:
    b4_verdict = "NO DATA"
    print("  No data.")

# ── Decision Rule ─────────────────────────────────────────────────────────────
print("\n=== DECISION RULE (using all available evidence) ===")

# B1 proxy: quote_flip.py fill latency = 18.4s median (confirmed over 300 markets, n=276)
b1_med_ms = 18358  # from quote_flip_full.py
b1_verdict = f"SLOW/PASSIVE proxy: fill-latency median {b1_med_ms/1000:.1f}s > 5s"

# B2 proxy: Phase 3 repricing 99.85% BUT this includes ALL participants
# The 88k updates/token/hr includes other makers. ohanism-specific updates unknown.
# Neutral signal.
b2_verdict = "AMBIGUOUS (level_changes includes all participants, not ohanism-only)"

b3_frac_high = (np.array(b3_corrs) > 0.7).mean() if b3_corrs else 0.5
b3_frac_low = (np.array(b3_corrs) < 0.2).mean() if b3_corrs else 0.5

print(f"  B1: {b1_verdict}")
print(f"  B2: {b2_verdict}")
print(f"  B3: {b3_verdict}")
print(f"  B4: {b4_verdict}")

passive_signals = 0
event_signals = 0

if b1_med_ms > 5000:
    passive_signals += 1
elif b1_med_ms < 500:
    event_signals += 1

if b3_frac_low > 0.7:
    passive_signals += 2  # strong signal
elif b3_frac_high > 0.7:
    event_signals += 2

b4_pull = counts.get("pull", 0) / len(b4_cases) * 100 if b4_cases else 1.0
if b4_pull < 1.0:
    passive_signals += 1  # very low pull → holds quotes (passive)

print(f"\n  Passive signals: {passive_signals}  Event-driven signals: {event_signals}")

if passive_signals >= 2 and event_signals == 0:
    decision = "PASSIVE. Keep gate R² ≥ 0.4 at quote-placement-time."
    gate_change = False
elif event_signals >= 3 and passive_signals == 0:
    decision = "EVENT-DRIVEN. Restore gate R² ≥ 0.6 at fill-time."
    gate_change = True
else:
    decision = f"HYBRID/AMBIGUOUS: passive={passive_signals} event={event_signals}. Log to BLOCKERS.md."
    gate_change = None

print(f"\n  DECISION: {decision}")

# Save results
results = {
    "b1": {"verdict": b1_verdict, "fill_latency_ms": b1_med_ms, "source": "quote_flip_full.py"},
    "b2": {"verdict": b2_verdict, "note": "level_changes not ohanism-specific"},
    "b3": {"verdict": b3_verdict, "n": b3_n,
           "frac_high": round(b3_frac_high, 3), "frac_low": round(b3_frac_low, 3)},
    "b4": {"verdict": b4_verdict, "cases": len(b4_cases), "pull_pct": round(b4_pull, 1)},
    "passive_signals": passive_signals,
    "event_signals": event_signals,
    "decision": decision,
    "gate_change": gate_change,
}
(cfg.results_dir / "part_b_fast.json").write_text(json.dumps(results, indent=2))
print("\nSaved: output/results/part_b_fast.json")
