"""Rebuild ohanism_fills.parquet with full market metadata from Gamma cache.

Two fixes vs initial attempt:
1. Join conflict: Gamma columns renamed before join to avoid suffix mess.
2. Binance bookTicker: e column is null for bookTicker rows — filter on e.is_null() & b.is_not_null().
"""
import sys
import time
from decimal import Decimal

sys.path.insert(0, "src")

import polars as pl
from pathlib import Path
from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
t0 = time.time()

fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills.parquet"))
print(f"Loaded {len(fills)} fills")

gamma_path = cfg.cache_dir / "gamma_token_lookup.parquet"
gamma = pl.read_parquet(str(gamma_path))
print(f"Gamma: {len(gamma)} tokens, direct match: "
      f"{len(set(gamma['token_id'].to_list()) & set(fills['token_id'].to_list()))}")

# Rename Gamma columns to avoid conflicts with existing null columns in fills
gamma_renamed = gamma.select([
    "token_id",
    pl.col("market").alias("_g_market"),
    pl.col("asset_symbol").alias("_g_asset_symbol"),
    pl.col("horizon").alias("_g_horizon"),
    pl.col("outcome_side").alias("_g_outcome_side"),
    pl.col("start_date_unix").alias("_g_start_date_unix"),
    pl.col("end_date_unix").alias("_g_end_date_unix"),
])

fills_e = fills.join(gamma_renamed, on="token_id", how="left")

fills_e = fills_e.with_columns([
    pl.when(pl.col("_g_market").is_not_null())
      .then(pl.col("_g_market"))
      .otherwise(pl.col("market"))
      .alias("market"),
    pl.col("_g_asset_symbol").alias("asset_symbol"),
    pl.col("_g_horizon").alias("horizon"),
    pl.col("_g_outcome_side").alias("outcome_side"),
    pl.when(pl.col("_g_end_date_unix").is_not_null())
      .then(pl.col("_g_end_date_unix") - pl.col("t_block_ns").cast(pl.Float64) / 1e9)
      .otherwise(pl.col("time_to_expiry_s"))
      .alias("time_to_expiry_s"),
]).drop(["_g_market", "_g_asset_symbol", "_g_horizon", "_g_outcome_side",
         "_g_end_date_unix", "_g_start_date_unix"])

covered = fills_e.filter(pl.col("asset_symbol").is_not_null()).height
print(f"Metadata coverage: {covered}/{len(fills_e)} ({covered/len(fills_e)*100:.1f}%)")
print(f"Horizon: {fills_e['horizon'].value_counts()}")
print(f"Asset:   {fills_e['asset_symbol'].value_counts()}")

# Build start_strike_price from Binance bookTicker
# bookTicker rows have e=null and b (bid) / a (ask) populated
dates = sorted(set(
    Path(p).parent.parent.name.replace("date=", "")
    for p in cfg.cache_dir.glob("feed=binance/date=*/hour=*/data.parquet")
))
print(f"\nBinance dates: {dates}")

SYMBOL_STREAM = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
}

# Build unique market → (asset_symbol, start_date_unix) lookup
market_lookup = (
    fills_e.filter(pl.col("_g_start_date_unix" if "_g_start_date_unix" in fills_e.columns else "asset_symbol").is_not_null())
    .unique(subset=["market"])
    .select(["market", "asset_symbol"])
    .join(
        gamma.unique(subset=["market"]).select(["market", "start_date_unix"]),
        on="market",
        how="left",
    )
    .filter(pl.col("asset_symbol").is_not_null() & pl.col("start_date_unix").is_not_null())
)

# Actually: rebuild properly from gamma
market_start = gamma.unique(subset=["market"]).select(["market", "asset_symbol", "start_date_unix"])

strike_rows: list[dict] = []
for sym, stream_base in SYMBOL_STREAM.items():
    sym_markets = market_start.filter(pl.col("asset_symbol") == sym)
    if sym_markets.is_empty():
        continue

    start_ns_arr = (sym_markets["start_date_unix"] * 1e9).cast(pl.Int64).to_list()
    min_ns = min(start_ns_arr) - 120_000_000_000  # 2min buffer
    max_ns = max(start_ns_arr) + 120_000_000_000

    ticker_frames = []
    for date in dates:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            # bookTicker: e is null, b and a populated
            df = lf.filter(
                pl.col("e").is_null()
                & pl.col("b").is_not_null()
                & pl.col("s").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream_base)
                & (pl.col("t_recv_ns") >= min_ns)
                & (pl.col("t_recv_ns") <= max_ns)
            ).collect()
            if len(df) > 0:
                ticker_frames.append(df)
        except FileNotFoundError:
            continue

    if not ticker_frames:
        print(f"  No bookTicker for {sym}")
        continue

    ticker = (
        pl.concat(ticker_frames)
        .with_columns(
            ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
        )
        .sort("t_recv_ns")
    )
    print(f"  {sym}: {len(ticker)} bookTicker rows")

    sym_m = sym_markets.with_columns(
        (pl.col("start_date_unix") * 1e9).cast(pl.Int64).alias("start_ns")
    ).sort("start_ns")

    joined = sym_m.join_asof(
        ticker.select(["t_recv_ns", "mid"]),
        left_on="start_ns",
        right_on="t_recv_ns",
        strategy="nearest",
    )

    for row in joined.iter_rows(named=True):
        mid = row.get("mid")
        if mid is not None:
            strike_str = str(Decimal(str(mid)).quantize(Decimal("0.000001")))
            strike_rows.append({"market": row["market"], "_strike": strike_str})

print(f"Strike prices built: {len(strike_rows)}")

if strike_rows:
    strikes_df = pl.DataFrame(strike_rows).unique(subset=["market"])
    fills_e = fills_e.join(
        strikes_df.rename({"_strike": "_g_strike"}),
        on="market",
        how="left",
    ).with_columns(
        pl.when(pl.col("_g_strike").is_not_null())
          .then(pl.col("_g_strike"))
          .otherwise(pl.col("start_strike_price"))
          .alias("start_strike_price")
    ).drop(["_g_strike"])

from reverse_engineering.tables.ohanism_fills import OHANISM_FILLS_COLUMNS
for c in OHANISM_FILLS_COLUMNS:
    if c not in fills_e.columns:
        fills_e = fills_e.with_columns(pl.lit(None).cast(pl.Utf8).alias(c))

fills_final = fills_e.select(OHANISM_FILLS_COLUMNS)
fills_final.write_parquet(str(cfg.tables_dir / "ohanism_fills.parquet"), compression="zstd")
print(f"\nWrote {len(fills_final)} rows in {time.time()-t0:.1f}s")
print(f"TTE null: {fills_final['time_to_expiry_s'].null_count()}")
print(f"strike null: {fills_final['start_strike_price'].null_count()}")
print(f"asset null: {fills_final['asset_symbol'].null_count()}")

btc = fills_final.filter(pl.col("asset_symbol") == "BTC").head(2)
if len(btc):
    print(f"BTC sample: price={btc['price'][:1].to_list()} "
          f"tte={btc['time_to_expiry_s'][:1].to_list()} "
          f"strike={btc['start_strike_price'][:1].to_list()}")
