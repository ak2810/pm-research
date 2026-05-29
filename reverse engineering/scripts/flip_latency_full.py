"""Extended flip-latency analysis on 300 markets for statistical adequacy.

Reports:
- Total ATM-crossing events in the sample
- Of those, how many had ohanism on BOTH sides (flip was observable)
- Latency distribution with bootstrap CI on the median
- Verdict on <50 sample threshold

Also counts: total 5m markets in window, ohanism's unique markets (selection rate).
"""
import sys
import time

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills.parquet"))

SYMBOL_STREAM = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
}
dates = ["2026-05-27", "2026-05-28"]

fills_with_strike = fills.filter(
    pl.col("start_strike_price").is_not_null()
    & pl.col("asset_symbol").is_not_null()
).with_columns(
    pl.col("start_strike_price").cast(pl.Float64).alias("strike_f")
)

market_info = (
    fills_with_strike.filter(pl.col("market").is_not_null())
    .group_by(["market", "asset_symbol"])
    .agg([
        pl.col("strike_f").first().alias("strike"),
        pl.col("t_block_ns").min().alias("t_start_ns"),
        pl.col("t_block_ns").max().alias("t_end_ns"),
        pl.col("outcome_side").unique().alias("sides_traded"),
    ])
    .filter(pl.col("asset_symbol").is_in(list(SYMBOL_STREAM)))
)

N_SAMPLE = 300
sample = market_info.sample(n=min(N_SAMPLE, len(market_info)), seed=42)
print(f"Sampling {len(sample)} markets (of {len(market_info)} unique with known strike)")

# Market selection fraction (Task 3 item)
total_5m_15m_in_window = fills.filter(
    pl.col("horizon").is_in(["5m", "15m"])
)["market"].drop_nulls().n_unique()

from reverse_engineering.io.gamma import _load_cached_cids
cached = _load_cached_cids()
total_gamma_markets = sum(1 for k in cached if k.startswith("slug:"))
print(f"\nMarket selection fraction:")
print(f"  Markets ohanism traded (5m+15m, with metadata): {total_5m_15m_in_window}")
print(f"  Total 5m/15m crypto markets in window (from Gamma cache): {total_gamma_markets}")
if total_gamma_markets > 0:
    selection_rate = total_5m_15m_in_window / total_gamma_markets
    print(f"  Selection rate: {selection_rate:.1%}")

t0 = time.time()
crossings_total = 0
crossings_both_sides = 0
latency_ms: list[float] = []
no_flip: int = 0

for row in sample.iter_rows(named=True):
    asset = row["asset_symbol"]
    strike = row["strike"]
    t_start = row["t_start_ns"]
    t_end = row["t_end_ns"]
    stream = SYMBOL_STREAM.get(asset, "")
    if not stream:
        continue

    buffer_ns = 30_000_000_000
    ticker_rows = []
    for date in dates:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null()
                & pl.col("b").is_not_null()
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

    for i in range(1, len(above)):
        if above[i] == above[i - 1]:
            continue
        crossings_total += 1
        c_time = int(t_arr[i])
        c_dir = "up" if above[i] else "down"
        new_favored = "Down" if c_dir == "up" else "Up"

        market_fills = fills_with_strike.filter(pl.col("market") == row["market"])
        if market_fills.is_empty():
            continue

        sides_present = set(market_fills["outcome_side"].drop_nulls().to_list())
        if len(sides_present) == 2:
            crossings_both_sides += 1

        after = market_fills.filter(
            (pl.col("t_block_ns") > c_time)
            & (pl.col("outcome_side") == new_favored)
        ).sort("t_block_ns")

        if not after.is_empty():
            lat = (int(after["t_block_ns"][0]) - c_time) / 1e6
            if 0 < lat < 120_000:
                latency_ms.append(lat)
        else:
            no_flip += 1

elapsed = time.time() - t0
print(f"\nProcessed {len(sample)} markets in {elapsed:.1f}s")
print(f"Total ATM crossings found: {crossings_total}")
print(f"Crossings where ohanism quoted BOTH sides: {crossings_both_sides}")
print(f"Flips measured (lat 0-120s): {len(latency_ms)}")
print(f"Crossings with no subsequent fill on new side: {no_flip}")

if latency_ms:
    lats = np.array(latency_ms)
    med = np.median(lats)
    # Bootstrap 95% CI on median
    rng = np.random.default_rng(42)
    boot_meds = [np.median(rng.choice(lats, size=len(lats), replace=True)) for _ in range(2000)]
    ci_lo, ci_hi = np.percentile(boot_meds, [2.5, 97.5])

    print(f"\n=== FLIP LATENCY (n={len(lats)}) ===")
    print(f"  median: {med:.0f}ms  [95% CI: {ci_lo:.0f}ms – {ci_hi:.0f}ms]")
    print(f"  p25={np.percentile(lats,25):.0f}ms  p75={np.percentile(lats,75):.0f}ms")
    print(f"  p90={np.percentile(lats,90):.0f}ms  p99={np.percentile(lats,99):.0f}ms")
    print(f"  min={lats.min():.0f}ms  max={lats.max():.0f}ms")

    if len(lats) >= 50:
        print(f"\n  Sample adequate (n={len(lats)} >= 50). Median reliable.")
    else:
        print(f"\n  Sample <50 (n={len(lats)}). Treat median as approximate.")

    print(f"\n=== VERDICT ===")
    if med < 500:
        print(f"  {med:.0f}ms < 500ms: EVENT-DRIVEN")
    elif med < 2000:
        print(f"  {med:.0f}ms in [500ms, 2s]: POLLING")
    else:
        print(f"  {med:.0f}ms > 2s: SLOW / PASSIVE")
