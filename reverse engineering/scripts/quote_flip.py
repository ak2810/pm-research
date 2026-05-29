"""Quote-flip discipline: does ohanism switch sides when spot crosses strike?

Algorithm:
1. For each (market, asset_symbol, start_strike_price) in ohanism_fills:
   - Load Binance bookTicker mid for that market's duration
   - Identify crossing moments: where mid crosses start_strike_price
     (= when Up price would cross 0.5, rebate-favored token flips)
2. Load pm_clob level_changes for that market's token_ids
3. For each crossing: find the next ohanism fill on each token side
4. Measure latency from crossing to first new fill on the new rebate-favored side

This tells us:
- Do they switch sides at all?
- How fast? (<500ms = event-driven, 500ms-2s = polling, >2s = slow)
"""
import sys
import time

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from pathlib import Path
from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed
from reverse_engineering.tables.level_changes import build_level_changes

cfg = get_settings()
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills.parquet"))

# Only markets with known strike (start_strike_price not null)
fills_with_strike = fills.filter(
    pl.col("start_strike_price").is_not_null()
    & pl.col("asset_symbol").is_not_null()
    & pl.col("horizon").is_not_null()
).with_columns(
    pl.col("start_strike_price").cast(pl.Float64).alias("strike_f"),
    pl.col("price").cast(pl.Float64).alias("price_f"),
)

# Get unique markets (token pairs) to process
# For crossing detection, group by market (condition_id) and asset_symbol
market_info = (
    fills_with_strike.filter(pl.col("market").is_not_null())
    .group_by(["market", "asset_symbol"])
    .agg([
        pl.col("token_id").first().alias("token_id_sample"),
        pl.col("strike_f").first().alias("strike"),
        pl.col("t_block_ns").min().alias("t_start_ns"),
        pl.col("t_block_ns").max().alias("t_end_ns"),
        pl.col("horizon").first().alias("horizon"),
    ])
    .filter(pl.col("asset_symbol").is_in(["BTC", "ETH", "SOL", "XRP", "DOGE"]))
)

SYMBOL_STREAM = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
}

dates = ["2026-05-27", "2026-05-28"]

print(f"Checking {len(market_info)} unique markets for spot crossings...")

crossings_found = 0
latency_ms_list: list[float] = []
flip_count = 0
no_flip_count = 0

# Sample: only do 100 markets (random seed 42) for speed
sample_markets = market_info.sample(n=min(100, len(market_info)), seed=42)

t0 = time.time()
for row in sample_markets.iter_rows(named=True):
    asset = row["asset_symbol"]
    strike = row["strike"]
    t_start = row["t_start_ns"]
    t_end = row["t_end_ns"]
    stream_base = SYMBOL_STREAM.get(asset, "")
    if not stream_base:
        continue

    # Load Binance bookTicker mid for this market's duration (+ 30s buffer)
    buffer_ns = 30_000_000_000
    min_ns = t_start - buffer_ns
    max_ns = t_end + buffer_ns

    ticker_rows = []
    for date in dates:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null()
                & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream_base)
                & (pl.col("t_recv_ns") >= min_ns)
                & (pl.col("t_recv_ns") <= max_ns)
            ).collect()
            if len(df) > 0:
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

    # Detect crossings: mid goes from below strike to above (or vice versa)
    mid_arr = ticker["mid"].to_numpy()
    t_arr = ticker["t_recv_ns"].to_numpy()
    above_strike = mid_arr > strike

    crossing_times: list[int] = []
    crossing_dirs: list[str] = []  # "up" or "down"
    for i in range(1, len(above_strike)):
        if above_strike[i] != above_strike[i - 1]:
            crossing_times.append(int(t_arr[i]))
            crossing_dirs.append("up" if above_strike[i] else "down")

    if not crossing_times:
        continue

    crossings_found += len(crossing_times)

    # For each crossing, check next ohanism fill for this market's tokens
    market_fills = fills_with_strike.filter(pl.col("market") == row["market"])
    if market_fills.is_empty():
        continue

    for c_time, c_dir in zip(crossing_times, crossing_dirs, strict=False):
        # Rebate-favored token after crossing:
        # c_dir="up" → mid > strike → Up>0.5 → Down is rebate-favored
        # c_dir="down" → mid < strike → Up<0.5 → Up is rebate-favored
        new_favored = "Down" if c_dir == "up" else "Up"

        # Look for ohanism's first fill on the new_favored token after crossing
        fills_after = market_fills.filter(
            (pl.col("t_block_ns") > c_time)
            & (pl.col("outcome_side") == new_favored)
        ).sort("t_block_ns")

        if not fills_after.is_empty():
            first_fill_ns = int(fills_after["t_block_ns"][0])
            lat_ms = (first_fill_ns - c_time) / 1e6
            if 0 < lat_ms < 60_000:  # sanity: 0-60s
                latency_ms_list.append(lat_ms)
                flip_count += 1
        else:
            no_flip_count += 1

elapsed = time.time() - t0
print(f"Processed {len(sample_markets)} markets in {elapsed:.1f}s")
print(f"Spot crossings found: {crossings_found}")
print(f"Crossings where ohanism switched to new rebate-favored side: {flip_count}")
print(f"Crossings where no subsequent fill on new side: {no_flip_count}")

if latency_ms_list:
    lats = np.array(latency_ms_list)
    print(f"\n=== FLIP LATENCY (ms from crossing to first fill on new rebate side) ===")
    print(f"  n={len(lats)}")
    print(f"  median={np.median(lats):.0f}ms  p25={np.percentile(lats,25):.0f}ms  p75={np.percentile(lats,75):.0f}ms")
    print(f"  p90={np.percentile(lats,90):.0f}ms  p99={np.percentile(lats,99):.0f}ms")
    print(f"  min={lats.min():.0f}ms  max={lats.max():.0f}ms")

    print(f"\n=== VERDICT ===")
    med = np.median(lats)
    if med < 500:
        print(f"  Median {med:.0f}ms < 500ms: ohanism is EVENT-DRIVEN. React to spot in real time.")
    elif med < 2000:
        print(f"  Median {med:.0f}ms = 500ms-2s: POLLING. Checks periodically.")
    else:
        print(f"  Median {med:.0f}ms > 2s: SLOW. Significant opportunity to front-run the flip.")

    if flip_count / max(crossings_found, 1) < 0.3:
        print(f"  Flip rate {flip_count/max(crossings_found,1):.1%}: low — ohanism often does NOT switch sides.")
    else:
        print(f"  Flip rate {flip_count/max(crossings_found,1):.1%}: moderate/high — does switch sides after crossing.")
else:
    print("\nNo valid flip latencies measured — all crossings had no subsequent fills.")
    print("This may mean: (a) crossings mostly near expiry (too late to refill),")
    print("               (b) sample too small, or (c) ohanism doesn't switch sides.")
