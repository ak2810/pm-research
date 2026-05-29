"""Phase 4 Step 4.1b (v2 fix): σ_implied at true quote-post time.

Attribution: earliest pm_clob price_change where:
  (a) size INCREASES from previous snapshot (new_order, not existing level), AND
  (b) ohanism subsequently fills at that same (token_id, price) within market lifetime.
No fallback — if no confirmed post found, drop the market. Clean > large.

Key fixes vs v1 attempt:
  - Track previous size per (token, price) level to detect genuine NEW orders
  - Only attribute to ohanism if a fill at the same price follows
  - No fallback to unconfirmed level appearances
  - Use fill price normalized to 6dp to match level_change prices
"""
import sys
import json
import time
from collections import defaultdict

sys.path.insert(0, "src")

import numpy as np
import polars as pl
from scipy.special import ndtri  # type: ignore[import-untyped]

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed
from reverse_engineering.tables.level_changes import _normalize_price

cfg = get_settings()

SECS_PER_YEAR = 365.25 * 24 * 3600
EPS_LOGSPOT   = 0.0001
EPS_PRICE_MID = 0.02
EPS_PRICE_BOUND = 0.02
SIGMA_CAP     = 15.0

SYMBOL_STREAM = {
    "BTC": "btcusdt", "ETH": "ethusdt",
    "SOL": "solusdt", "XRP": "xrpusdt", "DOGE": "dogeusdt",
}
DATES = ["2026-05-27", "2026-05-28", "2026-05-29"]

t0 = time.time()

# ── Load base data ────────────────────────────────────────────────────────────
sig_v1  = pl.read_parquet(str(cfg.tables_dir / "sigma_implied.parquet"))
fills   = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))

fills_meta = fills.filter(
    pl.col("market").is_not_null() & pl.col("t_ws_ns").is_not_null()
    & pl.col("t_block_ns").is_not_null()
).with_columns(pl.col("price").cast(pl.Float64).alias("price_f"))

market_meta = sig_v1.select([
    "market_id", "S0", "tau_years", "asset_symbol", "horizon", "t_quote_ns"
]).rename({"t_quote_ns": "t_first_fill_ns"})

market_tokens = (
    fills_meta.select([
        "market", "token_id", "t_block_ns", "time_to_expiry_s",
        "ohanism_side", "outcome_side", "price_f", "t_ws_ns"
    ])
    .rename({"market": "market_id"})
    .join(market_meta, on="market_id", how="inner")
)
market_tokens = market_tokens.with_columns(
    pl.when(pl.col("outcome_side") == "Up")
    .then(pl.col("price_f"))
    .otherwise(1.0 - pl.col("price_f"))
    .alias("p_canonical")
)

print(f"v1 markets: {len(sig_v1)}, fill-token rows: {len(market_tokens)}")

# ── Build token → pm_clob partition map ──────────────────────────────────────
print("Building token → pm_clob partition map...")
covered_tids: set[str] = set()
tid_to_date_hour: dict[str, tuple[str,int]] = {}
for parquet in sorted(cfg.cache_dir.glob("feed=pm_clob/date=*/hour=*/data.parquet")):
    date = parquet.parent.parent.name.replace("date=", "")
    hour = int(parquet.parent.name.replace("hour=", ""))
    lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False,
                          use_statistics=False)
    b = lf.filter(
        (pl.col("event_type") == "book") & pl.col("asset_id").is_not_null()
    ).select(["asset_id"]).collect()
    for tid in b["asset_id"].to_list():
        covered_tids.add(tid)
        if tid not in tid_to_date_hour:
            tid_to_date_hour[tid] = (date, hour)

fill_tids = set(market_tokens["token_id"].to_list())
covered_fill_tids = fill_tids & covered_tids
print(f"Fill tokens with pm_clob coverage: {len(covered_fill_tids)}/{len(fill_tids)}")

# Build per-token info for attribution
token_info: dict[str, dict] = {}
for row in market_tokens.iter_rows(named=True):
    tid = row["token_id"]
    if tid not in covered_fill_tids:
        continue
    tnb = int(row["t_block_ns"])
    horizon_val = row.get("horizon") or ""
    mkt_lifetime = {"5m": 300, "15m": 900, "1h": 3600}.get(horizon_val, 900)
    tau_s_here = float(row.get("time_to_expiry_s") or 0)
    # elapsed_in_market = time already passed since market start
    elapsed = max(0.0, mkt_lifetime - tau_s_here)
    # max lag = elapsed_so_far + one extra market_lifetime as pre-market buffer
    max_lag_s = elapsed + mkt_lifetime
    max_lag_ns = int(max_lag_s * 1e9)

    # ohanism SELL fill → they posted ASK → pm_clob side = "SELL"
    # ohanism BUY fill → they posted BID → pm_clob side = "BUY"
    ohanism_side_val = row.get("ohanism_side") or "SELL"
    pmclob_side = "SELL" if ohanism_side_val == "SELL" else "BUY"
    norm_p = _normalize_price(f"{row['price_f']:.6f}")

    if tid not in token_info:
        token_info[tid] = {
            "market_id": row["market_id"],
            "first_fill_ns": tnb,
            "fill_price_sides": {},  # norm_price → expected pmclob side
            "outcome_side": row["outcome_side"],
            "tau_s_at_first": tau_s_here,
            "max_lag_ns": max_lag_ns,
        }
    else:
        if tnb < token_info[tid]["first_fill_ns"]:
            token_info[tid]["first_fill_ns"] = tnb
            token_info[tid]["tau_s_at_first"] = tau_s_here
            token_info[tid]["max_lag_ns"] = max_lag_ns
    # Store price→side mapping (latest fill wins if multiple fills at same price)
    token_info[tid]["fill_price_sides"][norm_p] = pmclob_side

# ── Per pm_clob partition: find true post times ────────────────────────────────
partition_to_tids: dict[tuple[str,int], set[str]] = defaultdict(set)
for tid in covered_fill_tids:
    dh = tid_to_date_hour.get(tid)
    if dh:
        partition_to_tids[dh].add(tid)

print(f"Scanning {len(partition_to_tids)} pm_clob partitions...")

post_found: dict[str, dict] = {}  # token_id → {t_post_ns, p_posted, ...}

for (date, hour), tids in sorted(partition_to_tids.items()):
    if not tids:
        continue
    try:
        lf = scan_feed("pm_clob", date, hour,
                       columns=["event_type", "price_changes", "t_recv_ns"])
        pm_clob_df = lf.collect()
    except FileNotFoundError:
        continue

    # Track previous size per (token, price) to detect genuine size increases
    prev_size: dict[tuple[str,str], float] = {}
    # Confirmed new_orders per tid: list of (t_recv_ns, norm_price)
    new_orders_by_tid: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for row in pm_clob_df.filter(
        (pl.col("event_type") == "price_change") & pl.col("price_changes").is_not_null()
    ).iter_rows(named=True):
        t_ns = row["t_recv_ns"]
        try:
            entries = json.loads(row["price_changes"])
        except (json.JSONDecodeError, TypeError):
            continue
        for e in entries:
            tid = e.get("asset_id", "")
            if tid not in tids:
                continue
            tinfo = token_info.get(tid)
            if tinfo is None:
                continue
            if t_ns >= tinfo["first_fill_ns"]:
                continue  # only look BEFORE the first fill
            # Only look within max_lag_ns of first fill (rejects pre-market other-maker posts)
            if (tinfo["first_fill_ns"] - t_ns) > tinfo["max_lag_ns"]:
                continue

            size_str = e.get("size", "0")
            price_str = e.get("price", "0")
            try:
                size_f = float(size_str)
            except ValueError:
                continue

            entry_side = e.get("side", "")  # "BUY" (bid) or "SELL" (ask)
            norm_price = _normalize_price(price_str)
            key = (tid, norm_price, entry_side)
            prev = prev_size.get(key, 0.0)

            # Genuine NEW ORDER: size increases (new resting quote arrived)
            if size_f > prev + 0.001:
                # Only record if price+SIDE match a confirmed ohanism fill
                expected_side = tinfo["fill_price_sides"].get(norm_price)
                if expected_side is not None and entry_side == expected_side:
                    new_orders_by_tid[tid].append((t_ns, norm_price))

            prev_size[(tid, norm_price, entry_side)] = size_f

    # For each tid: take the earliest confirmed new_order
    for tid in tids:
        candidates = sorted(new_orders_by_tid.get(tid, []))
        if not candidates:
            continue  # NO FALLBACK — drop if no confirmed post found
        t_post_ns, p_norm = candidates[0]
        try:
            p_post_f = float(p_norm)
        except ValueError:
            continue

        tinfo = token_info[tid]
        outcome_side = tinfo["outcome_side"]
        p_canonical = p_post_f if outcome_side == "Up" else 1.0 - p_post_f

        post_found[tid] = {
            "t_post_ns": t_post_ns,
            "p_posted": p_canonical,
            "first_fill_ns": tinfo["first_fill_ns"],
            "tau_s_at_first": tinfo["tau_s_at_first"],
            "market_id": tinfo["market_id"],
        }

print(f"Tokens with confirmed post time: {len(post_found)} / {len(covered_fill_tids)}")

# ── Build σ_implied_v2 ────────────────────────────────────────────────────────
print("Loading Binance bookTicker...")
ticker_by_asset: dict[str, pl.DataFrame] = {}
for asset, stream in SYMBOL_STREAM.items():
    frames = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e","s","b","a","t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).with_columns(
                ((pl.col("b").cast(pl.Float64) + pl.col("a").cast(pl.Float64)) / 2.0).alias("mid")
            ).select(["t_recv_ns","mid"]).collect()
            if len(df):
                frames.append(df)
        except FileNotFoundError:
            continue
    if frames:
        ticker_by_asset[asset] = pl.concat(frames).sort("t_recv_ns")

# Flatten post_found to DataFrame, join metadata
post_rows_list = list(post_found.values())
if not post_rows_list:
    print("No post records found. Exiting.")
    import sys as _sys; _sys.exit(1)

post_df_raw = pl.DataFrame(post_rows_list)
post_df = post_df_raw.join(
    market_meta.join(sig_v1.select(["market_id","S0","tau_years"]), on="market_id", how="inner"),
    on="market_id", how="left"
)

results_v2 = []
drops2: dict[str,int] = {"atm_spot":0,"atm_price":0,"boundary":0,"sigma_cap":0,
                          "no_ticker":0,"no_S0":0}

for asset in SYMBOL_STREAM:
    ticker = ticker_by_asset.get(asset)
    asset_post = post_df.filter(pl.col("asset_symbol") == asset).sort("t_post_ns")
    if asset_post.is_empty() or ticker is None:
        continue

    joined = asset_post.join_asof(
        ticker.rename({"t_recv_ns":"t_tick","mid":"S_t"}),
        left_on="t_post_ns", right_on="t_tick", strategy="nearest"
    )

    for row in joined.iter_rows(named=True):
        S0 = row.get("S0")
        S_t = row.get("S_t")
        p   = row["p_posted"]
        t_post_ns     = row["t_post_ns"]
        first_fill_ns = row["first_fill_ns"]
        tau_s_first   = row.get("tau_s_at_first") or 0.0
        mkt = row["market_id"]

        if S0 is None or float(S0) <= 0:
            drops2["no_S0"] += 1; continue
        if S_t is None or float(S_t) <= 0:
            drops2["no_ticker"] += 1; continue

        lag_s = max(0.0, (first_fill_ns - t_post_ns) / 1e9)
        tau_s_post = float(tau_s_first) + lag_s
        if tau_s_post <= 0:
            drops2["atm_price"] += 1; continue
        tau_years = tau_s_post / SECS_PER_YEAR

        log_ratio = float(np.log(float(S0) / float(S_t)))
        if abs(log_ratio) < EPS_LOGSPOT:
            drops2["atm_spot"] += 1; continue
        if abs(p - 0.5) < EPS_PRICE_MID:
            drops2["atm_price"] += 1; continue
        if p < EPS_PRICE_BOUND or p > 1.0 - EPS_PRICE_BOUND:
            drops2["boundary"] += 1; continue

        phi_inv = float(ndtri(1.0 - p))
        if phi_inv == 0 or not np.isfinite(phi_inv):
            drops2["boundary"] += 1; continue

        sigma = log_ratio / (np.sqrt(tau_years) * phi_inv)
        if not np.isfinite(sigma) or sigma <= 0 or sigma > SIGMA_CAP:
            drops2["sigma_cap"] += 1; continue

        results_v2.append({
            "market_id": mkt,
            "asset_symbol": asset,
            "horizon": row.get("horizon",""),
            "t_post_ns": t_post_ns,
            "t_first_fill_ns": first_fill_ns,
            "post_to_fill_lag_s": lag_s,
            "tau_years": tau_years,
            "S0": float(S0),
            "S_t": float(S_t),
            "p_posted": p,
            "sigma_implied": sigma,
            "n_fills_in_market": 1,
        })

v2_df = pl.DataFrame(results_v2) if results_v2 else pl.DataFrame()
print(f"σ_implied_v2: {len(v2_df)} markets. Drops: {drops2}")

if not v2_df.is_empty():
    fill_counts = market_tokens.group_by("market_id").len().rename({"len":"nf"})
    v2_df = v2_df.join(fill_counts, on="market_id", how="left")
    v2_df = v2_df.with_columns(
        pl.col("nf").fill_null(1).alias("n_fills_in_market")
    ).drop("nf")
    out_path = cfg.tables_dir / "sigma_implied_v2.parquet"
    v2_df.write_parquet(str(out_path), compression="zstd")
    print(f"Written: {out_path}")

# ── Sanity checks ─────────────────────────────────────────────────────────────
print(f"\n=== S1: σ<0 count (v1=50) → v2={drops2['sigma_cap']} drops, 0 in dataset ===")

if not v2_df.is_empty():
    lag = v2_df["post_to_fill_lag_s"].drop_nulls().to_numpy()
    print(f"\n=== S2: Post-to-fill lag ===")
    print(f"  n={len(lag)}: median={np.median(lag):.1f}s p25={np.percentile(lag,25):.1f}s "
          f"p75={np.percentile(lag,75):.1f}s p90={np.percentile(lag,90):.1f}s max={lag.max():.1f}s")

    print(f"\n=== S3: σ_v2 distribution ===")
    stats = (
        v2_df.group_by(["asset_symbol","horizon"])
        .agg([pl.len().alias("n"),
              pl.col("sigma_implied").median().alias("median"),
              pl.col("sigma_implied").quantile(0.25).alias("p25"),
              pl.col("sigma_implied").quantile(0.75).alias("p75"),
              pl.col("sigma_implied").min().alias("min"),
              pl.col("sigma_implied").max().alias("max")])
        .sort(["asset_symbol","horizon"])
    )
    print(stats)
    btc5 = v2_df.filter(
        (pl.col("asset_symbol")=="BTC") & (pl.col("horizon")=="5m")
    )["sigma_implied"]
    med = float(btc5.median() or 0)
    print(f"BTC 5m σ_v2 median: {med:.3f}  {'PASS ✓' if 0.3<=med<=3.0 else 'WARNING'}")

    print(f"\n=== S4: v1 vs v2 correlation ===")
    common = v2_df.select(["market_id","sigma_implied"]).rename({"sigma_implied":"sv2"}).join(
        sig_v1.select(["market_id","sigma_implied"]).rename({"sigma_implied":"sv1"}),
        on="market_id", how="inner"
    ).drop_nulls()
    if len(common) >= 10:
        r = float(np.corrcoef(common["sv1"].to_numpy(), common["sv2"].to_numpy())[0,1])
        print(f"  n={len(common)}: Pearson r={r:.3f} {'✓' if 0.2<=r<=0.9 else 'WARNING'}")

print(f"\nStep 4.1b complete in {(time.time()-t0)/60:.1f} min")
