"""Phase 4 Step 4.2: Build candidate σ estimators at each quote-placement time.

For each market in sigma_implied.parquet, compute at t_quote_ns:
  - rv_W: realized vol (close-to-close log-returns on bookTicker) for W ∈ {1,5,15,30,60,240,1440} min
  - ewma_λ: EWMA on 1m log-returns for λ ∈ {0.90, 0.94, 0.97, 0.99}
  - garch: GARCH(1,1) on last 24h of 1m returns (pre-fitted per day, evaluated at t)
  - parkinson_30m, parkinson_1h: Parkinson estimator from kline_1m high/low
  - garman_klass_1h: Garman-Klass from kline_1m open/high/low/close
  - intraday_seasonal: hour-of-day average σ

Memory strategy: process per-asset, load Binance data once per asset per session.
All σ estimates are annualized (24/7, 31,557,600 s/year).

Output: output/tables/sigma_estimators.parquet keyed by market_id
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from arch import arch_model  # type: ignore[import-untyped]

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

warnings.filterwarnings("ignore")

cfg = get_settings()

SECS_PER_YEAR = 365.25 * 24 * 3600
MIN_SECS = 60
SYMBOL_STREAM = {
    "BTC": "btcusdt", "ETH": "ethusdt",
    "SOL": "solusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt",
}
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]

sigma_df = pl.read_parquet(str(cfg.tables_dir / "sigma_implied.parquet"))
print(f"σ_implied markets: {len(sigma_df)}")

t0 = time.time()
all_records = []

for asset, stream in SYMBOL_STREAM.items():
    asset_markets = sigma_df.filter(pl.col("asset_symbol") == asset)
    if asset_markets.is_empty():
        continue
    print(f"\n{asset}: {len(asset_markets)} markets")

    # Load bookTicker for this asset (mid prices, ns timestamps)
    ticker_rows = []
    kline_rows = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e", "s", "b", "a", "t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).with_columns(
                ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
            ).select(["t_recv_ns", "mid"]).collect()
            if len(df):
                ticker_rows.append(df)
        except FileNotFoundError:
            pass

        # kline_1m for Parkinson and Garman-Klass
        try:
            lf_k = scan_feed("binance", date, columns=["e", "s", "k", "t_recv_ns"])
            df_k = lf_k.filter(
                (pl.col("e") == "kline")
                & (pl.col("s").str.to_lowercase() == stream)
                & pl.col("k").is_not_null()
            ).collect()
            if len(df_k):
                kline_rows.append(df_k)
        except FileNotFoundError:
            pass

    if not ticker_rows:
        print(f"  No bookTicker data for {asset}")
        continue

    ticker = pl.concat(ticker_rows).sort("t_recv_ns")
    mids = ticker["mid"].to_numpy()
    ts_ns = ticker["t_recv_ns"].to_numpy()
    log_ret = np.diff(np.log(mids))  # log returns between consecutive ticks

    # Parse kline_1m
    klines_parsed = []
    if kline_rows:
        import json
        for df_k in kline_rows:
            for row in df_k.iter_rows(named=True):
                try:
                    k = json.loads(row["k"])
                    klines_parsed.append({
                        "t_open_ms": int(k["t"]),
                        "t_close_ms": int(k["T"]),
                        "open": float(k["o"]),
                        "high": float(k["h"]),
                        "low": float(k["l"]),
                        "close": float(k["c"]),
                    })
                except (KeyError, ValueError, TypeError):
                    pass

    klines_arr = None
    if klines_parsed:
        klines_df = pl.DataFrame(klines_parsed).sort("t_open_ms")
        klines_arr = klines_df.to_numpy()  # shape (N, 5)

    # Pre-fit GARCH(1,1) per 24h window using 1m returns
    # Build 1m return series from bookTicker
    print(f"  Building 1m returns for GARCH fitting...")
    # Resample to 1m grid using last tick in each minute
    ts_min = (ts_ns // (60 * 1_000_000_000)).astype(np.int64)
    unique_mins = np.unique(ts_min)

    min_prices = []
    for m in unique_mins:
        idx = np.where(ts_min == m)[0][-1]
        min_prices.append(mids[idx])

    min_prices_arr = np.array(min_prices)
    min_rets = np.diff(np.log(min_prices_arr))

    # Fit GARCH per 24h window (every 1440 1m-returns)
    print(f"  Fitting GARCH(1,1) rolling windows...")
    garch_h: dict[int, float] = {}  # minute_index → conditional variance h_t
    window = 1440
    for start in range(0, len(min_rets) - window, window // 2):
        chunk = min_rets[start: start + window]
        if len(chunk) < 100:
            continue
        try:
            am = arch_model(chunk * 100, vol="GARCH", p=1, q=1, rescale=False)
            res = am.fit(disp="off")
            # Store h_t for minutes in this chunk
            h_series = res.conditional_volatility ** 2  # variance per 1m step
            for i, h in enumerate(h_series):
                min_idx = start + i
                if min_idx < len(unique_mins):
                    garch_h[unique_mins[min_idx]] = float(h) * (1 / 100) ** 2
        except Exception:
            pass

    print(f"  GARCH fitted for {len(garch_h)} minute slots. Computing estimators...")

    # Intraday seasonality: per-hour average σ_rv_60m
    hour_sigma_arr: dict[int, float] = {}

    # Process each market
    for row in asset_markets.iter_rows(named=True):
        mkt = row["market_id"]
        t_q = row["t_quote_ns"]

        # Find index in ticker array closest to t_q
        idx_q = int(np.searchsorted(ts_ns, t_q))
        if idx_q >= len(ts_ns):
            idx_q = len(ts_ns) - 1

        rec: dict = {"market_id": mkt, "asset_symbol": asset, "horizon": row["horizon"]}

        # Realized volatility over trailing windows
        windows_min = [1, 5, 15, 30, 60, 240, 1440]
        for W_min in windows_min:
            W_ticks = max(1, int(W_min * 60 / 0.1))  # approx ticks (100ms avg)
            start_i = max(0, idx_q - W_ticks)
            if start_i >= idx_q:
                rec[f"rv_{W_min}m"] = np.nan
                continue
            ret_window = log_ret[start_i: min(idx_q, len(log_ret))]
            if len(ret_window) < 2:
                rec[f"rv_{W_min}m"] = np.nan
                continue
            dt_s = (ts_ns[idx_q] - ts_ns[start_i]) / 1e9
            if dt_s <= 0:
                rec[f"rv_{W_min}m"] = np.nan
                continue
            # Annualize: var = mean(r²) / dt_s_per_tick × secs_per_year
            mean_dt = dt_s / len(ret_window)
            rv_ann = float(np.sqrt(np.mean(ret_window ** 2) / mean_dt * SECS_PER_YEAR))
            rec[f"rv_{W_min}m"] = rv_ann

        # EWMA on 1m returns
        t_q_min = int(t_q // (60 * 1_000_000_000))
        idx_min_q = int(np.searchsorted(unique_mins, t_q_min))
        if idx_min_q > len(min_rets):
            idx_min_q = len(min_rets)

        for lam in [0.90, 0.94, 0.97, 0.99]:
            ret_1m = min_rets[max(0, idx_min_q - 1440): idx_min_q]
            if len(ret_1m) < 5:
                rec[f"ewma_{int(lam*100)}"] = np.nan
                continue
            h = float(np.var(ret_1m[:10]) if len(ret_1m) >= 10 else ret_1m[0] ** 2)
            for r in ret_1m:
                h = lam * h + (1 - lam) * float(r) ** 2
            # Annualize from 1m variance: h is per 1m step, multiply by 1440*365.25
            ev_ann = float(np.sqrt(h * 1440 * 365.25))
            rec[f"ewma_{int(lam*100)}"] = ev_ann

        # GARCH: look up h for the minute at t_q
        garch_val = garch_h.get(t_q_min)
        if garch_val is not None:
            rec["garch"] = float(np.sqrt(garch_val * 1440 * 365.25))
        else:
            rec["garch"] = np.nan

        # Parkinson and Garman-Klass from kline_1m
        if klines_arr is not None and len(klines_arr) > 0:
            t_q_ms = t_q // 1_000_000
            k_ts = klines_df["t_open_ms"].to_numpy()
            idx_k = int(np.searchsorted(k_ts, t_q_ms))
            for label, n_candles in [("park_30m", 30), ("park_1h", 60), ("gk_1h", 60)]:
                start_k = max(0, idx_k - n_candles)
                subset = klines_df.slice(start_k, min(idx_k - start_k, n_candles))
                if len(subset) < 5:
                    rec[label] = np.nan
                    continue
                H = subset["high"].to_numpy()
                L = subset["low"].to_numpy()
                O = subset["open"].to_numpy()
                C = subset["close"].to_numpy()
                if label.startswith("park"):
                    ln_hl2 = np.log(H / L) ** 2
                    park_var = np.mean(ln_hl2) / (4 * np.log(2))
                    rec[label] = float(np.sqrt(park_var * 1440 * 365.25))
                else:  # garman-klass
                    gk_var = np.mean(
                        0.5 * np.log(H / L) ** 2
                        - (2 * np.log(2) - 1) * np.log(C / O) ** 2
                    )
                    rec[label] = float(np.sqrt(max(0, gk_var) * 1440 * 365.25))
        else:
            for label in ["park_30m", "park_1h", "gk_1h"]:
                rec[label] = np.nan

        # Intraday seasonal: hour-of-UTC
        hour_utc = (t_q // 3_600_000_000_000) % 24
        if hour_utc not in hour_sigma_arr:
            # Compute hourly average of rv_60m
            hour_rvs = []
            for hr_row in asset_markets.iter_rows(named=True):
                if (hr_row["t_quote_ns"] // 3_600_000_000_000) % 24 == hour_utc:
                    hour_rvs.append(rec.get("rv_60m", np.nan))
            hour_sigma_arr[int(hour_utc)] = float(np.nanmean(hour_rvs)) if hour_rvs else np.nan
        rec["intraday_seasonal"] = hour_sigma_arr.get(int(hour_utc), np.nan)

        all_records.append(rec)

    print(f"  {asset}: {len([r for r in all_records if r['asset_symbol']==asset])} records computed")

if not all_records:
    print("No records! Check data.")
else:
    df_out = pl.DataFrame(all_records)
    out_path = cfg.tables_dir / "sigma_estimators.parquet"
    df_out.write_parquet(str(out_path), compression="zstd")
    print(f"\nWritten: {out_path} ({len(df_out)} rows, {len(df_out.columns)} columns)")

    # Quick coverage check
    print("\n=== ESTIMATOR COVERAGE (non-null %) ===")
    estimator_cols = [c for c in df_out.columns if c not in ("market_id", "asset_symbol", "horizon")]
    for col in estimator_cols:
        pct = df_out[col].drop_nulls().len() / len(df_out) * 100
        if pct < 80:
            print(f"  {col}: {pct:.1f}% ← LOW")
        else:
            print(f"  {col}: {pct:.1f}%")

    # Sample stats for BTC 5m
    btc5 = df_out.filter(
        (pl.col("asset_symbol") == "BTC") & (pl.col("horizon") == "5m")
    )
    print(f"\nBTC 5m sample (n={len(btc5)}):")
    for col in ["rv_5m", "rv_60m", "ewma_97", "garch", "park_30m"]:
        if col in btc5.columns:
            med = btc5[col].drop_nulls().median()
            print(f"  {col}: median={med:.3f}" if med is not None else f"  {col}: no data")

    print(f"\nStep 4.2 complete in {(time.time()-t0)/60:.1f} min")
