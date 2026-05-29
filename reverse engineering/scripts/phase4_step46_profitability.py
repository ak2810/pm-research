"""Phase 4 Step 4.6: Profitability decomposition per fill.

Per-fill P&L = rebate + position_MTM_at_resolution - adverse_selection_cost - fees
  rebate: already in fills.rebate_received
  MTM at resolution: requires ConditionResolution outcome from polygon + Binance close
  Adverse selection: signed spot move from t_post_ns to t_fill_ns × position × direction
  Fees: all maker fills have fee=0 (confirmed Phase 1)

Uses sigma_implied_v2 to get t_post_ns for 997 markets; fills for the full set.
MTM uses end_date_unix + Binance settle price.
"""
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np
import polars as pl

from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
SECS_PER_YEAR = 365.25 * 24 * 3600
SYMBOL_STREAM = {"BTC":"btcusdt","ETH":"ethusdt","SOL":"solusdt","XRP":"xrpusdt","DOGE":"dogeusdt"}
DATES = ["2026-05-27","2026-05-28","2026-05-29"]

t0 = time.time()

# ── Load data ─────────────────────────────────────────────────────────────────
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))
sig_v2 = pl.read_parquet(str(cfg.tables_dir / "sigma_implied_v2.parquet"))
sig_v2 = sig_v2.unique(subset=["market_id"]).select(
    ["market_id","t_post_ns","S0"]
).rename({"market_id":"market","S0":"strike"})

print(f"Fills: {len(fills)}, σ_v2 markets: {len(sig_v2)}")

# ── Join post-time info to fills ──────────────────────────────────────────────
fills_w = fills.filter(
    pl.col("market").is_not_null()
    & pl.col("start_strike_price").is_not_null()
    & pl.col("t_block_ns").is_not_null()
    & pl.col("asset_symbol").is_not_null()
    & pl.col("time_to_expiry_s").is_not_null()
).with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
    pl.col("rebate_received").cast(pl.Float64).alias("rebate_f"),
    pl.col("time_to_expiry_s").cast(pl.Float64).alias("tau_s"),
    pl.col("start_strike_price").cast(pl.Float64).alias("S0"),
])
# Add canonical_sign (+1=long-Up, -1=short-Up)
fills_w = fills_w.with_columns(
    pl.when(
        ((pl.col("ohanism_side")=="BUY")&(pl.col("outcome_side")=="Up"))
        | ((pl.col("ohanism_side")=="SELL")&(pl.col("outcome_side")=="Down"))
    ).then(pl.lit(1.0)).otherwise(pl.lit(-1.0)).alias("canonical_sign")
)
# Join post time
fills_w = fills_w.join(sig_v2, on="market", how="left")

print(f"Fills with post-time: {fills_w['t_post_ns'].drop_nulls().len()}/{len(fills_w)}")

# ── Binance close at resolution (for MTM) ────────────────────────────────────
print("Building Binance close prices at resolution...")
ticker_by_asset: dict[str, pl.DataFrame] = {}
for asset, stream in SYMBOL_STREAM.items():
    asset_fills = fills_w.filter(pl.col("asset_symbol") == asset)
    if asset_fills.is_empty(): continue
    frames = []
    for date in DATES:
        try:
            lf = scan_feed("binance", date, columns=["e","s","b","a","t_recv_ns"])
            df = lf.filter(
                pl.col("e").is_null() & pl.col("b").is_not_null()
                & (pl.col("s").str.to_lowercase() == stream)
            ).with_columns(
                ((pl.col("b").cast(pl.Float64)+pl.col("a").cast(pl.Float64))/2.0).alias("mid")
            ).select(["t_recv_ns","mid"]).collect()
            if len(df): frames.append(df)
        except FileNotFoundError: pass
    if frames: ticker_by_asset[asset] = pl.concat(frames).sort("t_recv_ns")

# Compute per-fill P&L components
print("Computing P&L per fill...")
pnl_rows = []

for asset in SYMBOL_STREAM:
    ticker = ticker_by_asset.get(asset)
    asset_fills = fills_w.filter(pl.col("asset_symbol") == asset)
    if asset_fills.is_empty() or ticker is None:
        continue

    ts_ticker = ticker["t_recv_ns"].to_numpy()
    mid_ticker = ticker["mid"].to_numpy()

    for row in asset_fills.iter_rows(named=True):
        t_fill = row["t_block_ns"]
        t_post = row.get("t_post_ns")
        # Compute end_unix from fills: t_block_ns/1e9 + time_to_expiry_s
        tau_at_fill = row["tau_s"] or 0
        end_unix = (t_fill / 1e9) + tau_at_fill if tau_at_fill > 0 else None
        S0_strike = float(row.get("strike") or row.get("start_strike_price") or 0)
        if S0_strike == 0: S0_strike = None
        canonical_sign = row.get("canonical_sign", 0)
        price_f = row["price_f"]
        size_f = row["size_f"]
        rebate_f = row["rebate_f"]
        tau_s = row["tau_s"]

        # Spot at fill time
        idx_fill = int(np.searchsorted(ts_ticker, t_fill))
        if idx_fill >= len(ts_ticker): idx_fill = len(ts_ticker) - 1
        S_fill = float(mid_ticker[idx_fill])

        # Spot at post time (if available)
        S_post = None
        if t_post:
            idx_post = int(np.searchsorted(ts_ticker, t_post))
            if idx_post >= len(ts_ticker): idx_post = len(ts_ticker) - 1
            S_post = float(mid_ticker[idx_post])

        # MTM at resolution: spot at end_unix
        S_resolve = None
        if end_unix and end_unix > 0:
            t_end_ns = int(end_unix * 1e9)
            idx_end = int(np.searchsorted(ts_ticker, t_end_ns))
            if idx_end >= len(ts_ticker): idx_end = len(ts_ticker) - 1
            S_resolve = float(mid_ticker[idx_end])

        # MTM P&L: if long Up (canonical_sign=+1), Win if S_resolve > S0
        # P&L_MTM = canonical_sign × (1 if S_resolve > S0 else 0) × size - canonical_sign × price × size
        # = position × (outcome - price) × size
        # Actually: ohanism sells (or buys) a token at price_f, token pays 1 if Up (or Down).
        # For canonical Up perspective:
        #   canonical_sign=+1 (long Up): ohanism RECEIVES 1 USDC per token if S_resolve>S0
        #   canonical_sign=-1 (short Up): ohanism RECEIVES 1 USDC per token if S_resolve<S0 (long Down)
        # Net MTM (per token): (1 if winning else 0) - price_f
        # Total MTM = canonical_sign × ((1 if S_resolve>S0 else 0) - price_f) × size_f

        mtm = None
        if S_resolve is not None and S0_strike > 0:
            up_wins = 1.0 if S_resolve > S0_strike else 0.0
            # For canonical_sign=+1 (long Up): paid price_f, gets up_wins
            # For canonical_sign=-1 (short Up = long Down): paid (1-price_f) for Down token
            #   Down wins = (1-up_wins). Net = (1-up_wins) - (1-price_f) = price_f - up_wins
            #   = -(up_wins - price_f)
            # Unified: MTM = canonical_sign × (up_wins - price_f) × size_f
            mtm = float(canonical_sign * (up_wins - price_f) * size_f)

        # Adverse selection: signed spot move from post to fill, against position
        # AS_cost = canonical_sign × (S_fill/S_post - 1) (if spot moves in canonical direction = adverse)
        # But for a passive quoter:
        # - canonical_sign=+1 (long Up): if spot goes UP from post to fill (Up winning more), taker was informed → adverse
        # - AS_cost = canonical_sign × (S_fill - S_post) / S_post × size_f × price_f (in USDC approx)
        as_cost = None
        if S_post is not None and S_post > 0:
            spot_move_pct = (S_fill - S_post) / S_post
            # Adverse selection: if spot moved in canonical direction (ohanism's side)
            # For canonical_sign=+1 (long Up), spot going UP is BAD for AS (taker was right)
            # AS cost = canonical_sign × spot_move_pct × notional
            notional = price_f * size_f
            as_cost = float(canonical_sign * spot_move_pct * notional)

        pnl_rows.append({
            "asset": asset,
            "horizon": row.get("horizon",""),
            "ohanism_side": row.get("ohanism_side",""),
            "canonical_sign": float(canonical_sign),
            "price": price_f,
            "size": size_f,
            "rebate": float(rebate_f) if rebate_f is not None else float("nan"),
            "mtm": float(mtm) if mtm is not None else float("nan"),
            "as_cost": float(as_cost) if as_cost is not None else float("nan"),
            "tau_s_at_fill": float(tau_s) if tau_s is not None else float("nan"),
        })

pnl_df = pl.DataFrame(pnl_rows) if pnl_rows else pl.DataFrame()
print(f"P&L rows computed: {len(pnl_df)}")

if pnl_df.is_empty():
    print("ERROR: No P&L rows!")
    exit()

# Filter to fills with complete data
pnl_full = pnl_df.filter(
    pl.col("mtm").is_finite() & pl.col("as_cost").is_finite()
)
print(f"Fills with complete P&L (MTM+AS): {len(pnl_full)}")
pnl_rebate_only = pnl_df.filter(pl.col("rebate").is_finite())
print(f"Fills with rebate data: {len(pnl_rebate_only)}")

# ── Aggregate P&L ─────────────────────────────────────────────────────────────
print("\n=== PROFITABILITY DECOMPOSITION ===")

if len(pnl_full) > 0:
    total_rebate = float(pnl_full["rebate"].sum())
    total_mtm    = float(pnl_full["mtm"].sum())
    total_as     = float(pnl_full["as_cost"].sum())
    total_fees   = 0.0  # all maker fills have fee=0 (Phase 1 confirmed)
    net_pnl      = total_rebate + total_mtm - total_as - total_fees

    n_full = len(pnl_full)
    print(f"N={n_full} fills (complete P&L)")
    print(f"  Rebate:            {total_rebate:+.2f} USDC  ({total_rebate/n_full*100:.3f}% per fill)")
    print(f"  MTM at resolution: {total_mtm:+.2f} USDC  ({total_mtm/n_full*100:.3f}% per fill)")
    print(f"  Adverse selection: {total_as:+.2f} USDC  ({total_as/n_full*100:.3f}% per fill)")
    print(f"  Fees:              {total_fees:.2f} USDC  (confirmed zero for maker fills)")
    print(f"  NET P&L:           {net_pnl:+.2f} USDC  ({'POSITIVE ✓' if net_pnl>0 else 'NEGATIVE ✗'})")
    print()

    # Per-fill distributions
    rebate_arr = pnl_full["rebate"].to_numpy()
    mtm_arr    = pnl_full["mtm"].to_numpy()
    as_arr     = pnl_full["as_cost"].to_numpy()
    net_arr    = rebate_arr + mtm_arr - as_arr

    print("Per-fill distributions:")
    print(f"  Rebate: mean={np.mean(rebate_arr):.4f} median={np.median(rebate_arr):.4f} std={np.std(rebate_arr):.4f}")
    print(f"  MTM:    mean={np.mean(mtm_arr):.4f} median={np.median(mtm_arr):.4f} std={np.std(mtm_arr):.4f}")
    print(f"  AS:     mean={np.mean(as_arr):.4f} median={np.median(as_arr):.4f} std={np.std(as_arr):.4f}")
    print(f"  Net:    mean={np.mean(net_arr):.4f} median={np.median(net_arr):.4f} std={np.std(net_arr):.4f}")
    print(f"  Fraction positive net: {(net_arr>0).mean()*100:.1f}%")

    # Decomposition as percentages
    gross = total_rebate + abs(total_mtm) + abs(total_as)
    if gross > 0:
        print(f"\nP&L source decomposition (% of gross flow):")
        print(f"  Rebate contribution: {total_rebate/gross*100:.1f}%")
        print(f"  MTM contribution:    {total_mtm/gross*100:.1f}%")
        print(f"  AS cost:             {-total_as/gross*100:.1f}%")

    # Per-asset breakdown
    print("\nPer-asset breakdown:")
    for asset in ["BTC","ETH","SOL","XRP","DOGE"]:
        a_mask = pnl_full["asset"] == asset
        n_a = a_mask.sum()
        if n_a < 5: continue
        a_df = pnl_full.filter(pl.col("asset")==asset)
        r_a = a_df["rebate"].sum(); m_a = a_df["mtm"].sum(); as_a = a_df["as_cost"].sum()
        net_a = float(r_a + m_a - as_a)
        print(f"  {asset}: n={n_a} rebate={r_a:.1f} MTM={m_a:.1f} AS={as_a:.1f} net={net_a:+.1f}")

elif len(pnl_rebate_only) > 0:
    print("Only rebate data available (no MTM/AS — missing resolution data)")
    total_rebate = float(pnl_rebate_only["rebate"].sum())
    print(f"Total rebate (all fills): {total_rebate:.2f} USDC")

# Save
results_pnl = {
    "n_complete": int(len(pnl_full)) if len(pnl_full)>0 else 0,
    "n_rebate_only": int(len(pnl_rebate_only)),
}
if len(pnl_full) > 0:
    results_pnl.update({
        "total_rebate_usdc": float(total_rebate),
        "total_mtm_usdc": float(total_mtm),
        "total_as_usdc": float(total_as),
        "net_pnl_usdc": float(net_pnl),
        "net_positive": bool(net_pnl > 0),
        "mean_rebate_per_fill": float(np.mean(rebate_arr)),
        "mean_mtm_per_fill": float(np.mean(mtm_arr)),
        "mean_as_per_fill": float(np.mean(as_arr)),
        "pct_positive_net": float((net_arr>0).mean()*100),
    })
(cfg.results_dir / "phase4_profitability.json").write_text(json.dumps(results_pnl, indent=2))
print(f"\nSaved: output/results/phase4_profitability.json")
print(f"Total runtime: {(time.time()-t0)/60:.1f} min")
