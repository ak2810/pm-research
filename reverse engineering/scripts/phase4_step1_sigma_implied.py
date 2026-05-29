"""Phase 4 Step 4.1: Build σ_implied dataset.

For each MARKET (not each fill), identify the earliest ohanism fill as the
quote-placement-time proxy, then invert the digital-option formula to get
ohanism's implied σ at that moment.

Annualization: τ in years = τ_seconds / 31,557,600 (24/7 calendar year).
ε thresholds:
  |log(S_0/S_t)| < 0.0001 → drop (ATM at quote time, σ degenerate)
  |p_quoted - 0.5| < 0.02  → drop (near ATM in prob space)
  p_quoted < 0.02 or > 0.98 → drop (near 0/1, σ blows up)
  |σ_implied| > 15          → drop (degenerate)

Output: output/tables/sigma_implied.parquet
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from scipy.special import ndtri  # type: ignore[import-untyped]
from scipy.stats import norm  # type: ignore[import-untyped]

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
cfg.tables_dir.mkdir(parents=True, exist_ok=True)

SECS_PER_YEAR = 365.25 * 24 * 3600  # 24/7 calendar year
EPS_LOGSPOT = 0.0001   # |log(S_0/S_t)| below this → ATM, σ undefined
EPS_PRICE_MID = 0.02   # |p - 0.5| below this → near-ATM prob, σ noisy
EPS_PRICE_BOUNDARY = 0.02  # p < this or > 1-this → σ blows up
SIGMA_CAP = 15.0       # annualized σ sanity cap

SYMBOL_STREAM = {
    "BTC": "btcusdt", "ETH": "ethusdt",
    "SOL": "solusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt",
}
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]

t0 = time.time()

# Load fills with full metadata
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
fills_meta = fills.filter(
    pl.col("asset_symbol").is_not_null()
    & pl.col("start_strike_price").is_not_null()
    & pl.col("time_to_expiry_s").is_not_null()
    & pl.col("t_block_ns").is_not_null()
    & pl.col("market").is_not_null()
    & (pl.col("time_to_expiry_s").cast(pl.Float64) > 0)
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("start_strike_price").cast(pl.Float64).alias("S0"),
    pl.col("time_to_expiry_s").cast(pl.Float64).alias("tau_s"),  # already = end - t_block/1e9
    pl.col("t_block_ns").cast(pl.Float64).alias("t_block_f"),
])

# Canonical Up-equivalent price
fills_meta = fills_meta.with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("p_canonical")
)

print(f"Fills with full metadata: {len(fills_meta):,}")

# Per-market: take the EARLIEST fill (proxy quote-placement-time)
# Group by market, get row with min t_block_ns
first_fills = (
    fills_meta
    .sort("t_block_ns")
    .group_by("market")
    .agg([
        pl.first("asset_symbol").alias("asset_symbol"),
        pl.first("horizon").alias("horizon"),
        pl.first("t_block_ns").alias("t_quote_ns"),
        pl.first("tau_s").alias("tau_s"),
        pl.first("S0").alias("S0"),
        pl.first("p_canonical").alias("p_quoted"),
        pl.len().alias("n_fills_in_market"),
    ])
)
print(f"Unique markets: {len(first_fills):,}")

# Load Binance bookTicker for all assets (one per-asset pass)
print("Loading Binance bookTicker data...")
ticker_by_asset: dict[str, pl.DataFrame] = {}
for asset, stream in SYMBOL_STREAM.items():
    frames = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null()
                & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).with_columns(
                ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
            ).select(["t_recv_ns", "mid"]).collect()
            if len(df):
                frames.append(df)
        except FileNotFoundError:
            continue
    if frames:
        ticker_by_asset[asset] = pl.concat(frames).sort("t_recv_ns")
        print(f"  {asset}: {len(ticker_by_asset[asset]):,} bookTicker rows")

# Per-asset: join nearest Binance mid at t_quote_ns
records = []
drops = {"atm_spot": 0, "atm_price": 0, "boundary_price": 0, "sigma_cap": 0, "no_ticker": 0}
kept = 0

for asset in SYMBOL_STREAM:
    ticker = ticker_by_asset.get(asset)
    if ticker is None:
        drops["no_ticker"] += len(first_fills.filter(pl.col("asset_symbol") == asset))
        continue

    asset_markets = first_fills.filter(pl.col("asset_symbol") == asset).sort("t_quote_ns")
    if asset_markets.is_empty():
        continue

    # join_asof to find nearest bookTicker at t_quote_ns
    joined = asset_markets.join_asof(
        ticker.rename({"t_recv_ns": "t_ticker_ns", "mid": "S_t"}),
        left_on="t_quote_ns",
        right_on="t_ticker_ns",
        strategy="nearest",
    )

    for row in joined.iter_rows(named=True):
        S0 = row["S0"]
        S_t = row.get("S_t")
        p = row["p_quoted"]
        tau_s_val = row["tau_s"]   # already = end_date_unix - t_block_ns/1e9
        t_quote_ns = row["t_quote_ns"]
        market = row["market"]

        if S_t is None or S_t <= 0 or S0 <= 0:
            drops["no_ticker"] += 1
            continue

        tau_s = tau_s_val  # already computed: end_date_unix - t_block_ns/1e9
        if tau_s is None or tau_s <= 0:
            drops["atm_price"] += 1
            continue

        tau_years = tau_s / SECS_PER_YEAR
        log_ratio = float(np.log(S0 / S_t))

        # Drop ATM-spot
        if abs(log_ratio) < EPS_LOGSPOT:
            drops["atm_spot"] += 1
            continue

        # Drop near-ATM probability
        if abs(p - 0.5) < EPS_PRICE_MID:
            drops["atm_price"] += 1
            continue

        # Drop boundary probability
        if p < EPS_PRICE_BOUNDARY or p > 1.0 - EPS_PRICE_BOUNDARY:
            drops["boundary_price"] += 1
            continue

        # Invert: σ = log(S0/S_t) / (√τ × Φ⁻¹(1−p))
        phi_inv = float(ndtri(1.0 - p))  # Φ⁻¹(1-p)
        if phi_inv == 0 or not np.isfinite(phi_inv):
            drops["boundary_price"] += 1
            continue

        sigma = log_ratio / (np.sqrt(tau_years) * phi_inv)

        # Drop degenerate or physically impossible
        # σ < 0 occurs when spot moved against ohanism's opening direction before
        # the first fill — artifact of using first-fill as quote-placement proxy.
        if not np.isfinite(sigma) or abs(sigma) > SIGMA_CAP or sigma <= 0:
            drops["sigma_cap"] += 1
            continue

        records.append({
            "market_id": market,
            "asset_symbol": asset,
            "horizon": row["horizon"],
            "t_quote_ns": t_quote_ns,
            "tau_years": tau_years,
            "S0": S0,
            "S_t": float(S_t),
            "p_quoted": p,
            "sigma_implied": sigma,
            "n_fills_in_market": row["n_fills_in_market"],
        })
        kept += 1

print(f"\nRetained: {kept:,} markets")
print(f"Dropped — atm_spot: {drops['atm_spot']}, atm_price: {drops['atm_price']}, "
      f"boundary: {drops['boundary_price']}, sigma_cap: {drops['sigma_cap']}, "
      f"no_ticker: {drops['no_ticker']}")

sigma_df = pl.DataFrame(records)
out_path = cfg.tables_dir / "sigma_implied.parquet"
sigma_df.write_parquet(str(out_path), compression="zstd")
print(f"\nWritten: {out_path} ({len(sigma_df)} rows)")

# Sanity checks per (asset, horizon)
print("\n=== σ_implied DISTRIBUTION BY (asset, horizon) ===")
stats = (
    sigma_df.group_by(["asset_symbol", "horizon"])
    .agg([
        pl.len().alias("n"),
        pl.col("sigma_implied").median().alias("median"),
        pl.col("sigma_implied").quantile(0.25).alias("p25"),
        pl.col("sigma_implied").quantile(0.75).alias("p75"),
        pl.col("sigma_implied").min().alias("min"),
        pl.col("sigma_implied").max().alias("max"),
        pl.col("sigma_implied").std().alias("std"),
    ])
    .with_columns(
        (pl.col("p75") - pl.col("p25")).alias("IQR")
    )
    .sort(["asset_symbol", "horizon"])
)
print(stats)

# Retention rates
print("\n=== RETENTION RATE ===")
total_by_asset = first_fills.group_by("asset_symbol").len()
retained_by_asset = sigma_df.group_by("asset_symbol").len()
for row in total_by_asset.iter_rows(named=True):
    asset = row["asset_symbol"]
    total = row["len"]
    ret = retained_by_asset.filter(pl.col("asset_symbol") == asset)["len"][0] if not retained_by_asset.filter(pl.col("asset_symbol") == asset).is_empty() else 0
    print(f"  {asset}: {ret}/{total} ({ret/total*100:.1f}%)")

# PLAUSIBILITY CHECK
btc_5m = sigma_df.filter(
    (pl.col("asset_symbol") == "BTC") & (pl.col("horizon") == "5m")
)["sigma_implied"]
if len(btc_5m) > 0:
    med = float(btc_5m.median() or 0)
    print(f"\nBTC 5m σ_implied median: {med:.3f} annualized")
    if 0.3 <= med <= 3.0:
        print("  PLAUSIBILITY: OK (in [0.3, 3.0] range)")
    else:
        print(f"  WARNING: Outside [0.3, 3.0] — check τ units / canonical p convention!")

print(f"\nStep 4.1 complete in {(time.time()-t0)/60:.1f} min")
