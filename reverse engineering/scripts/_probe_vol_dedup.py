"""Diagnostic: compare our fills volume to leaderboard vol=270,158 USDC.
Also check for duplicate transaction hashes (would cause 2x P&L overcount).
"""
import sys
sys.path.insert(0, "src")
import polars as pl
from reverse_engineering.config import get_settings

cfg = get_settings()
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))

print(f"Total fills: {len(fills)}")

# 1. Unique transaction hashes
if "order_hash" in fills.columns:
    tx_col = "order_hash"
elif "tx_hash" in fills.columns:
    tx_col = "tx_hash"
elif "transaction_hash" in fills.columns:
    tx_col = "transaction_hash"
else:
    # find hash-like columns
    tx_col = None
    for c in fills.columns:
        if "hash" in c.lower():
            tx_col = c
            break

print(f"\nHash column: {tx_col}")
if tx_col:
    n_unique = fills[tx_col].n_unique()
    print(f"Unique {tx_col}:  {n_unique}")
    print(f"Total fills:     {len(fills)}")
    dup_rate = 1.0 - n_unique / len(fills)
    print(f"Duplicate rate:  {dup_rate*100:.2f}%")

# 2. Compute total usdcSize = price * size (our window volume)
fills_w = fills.with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
]).with_columns(
    (pl.col("price_f") * pl.col("size_f")).alias("usdc_size")
)
total_usdc = float(fills_w["usdc_size"].sum())
print(f"\nOur window vol (sum price*size): {total_usdc:,.2f} USDC")
print(f"Leaderboard lifetime vol:         270,158.65 USDC")
ratio = total_usdc / 270158.65
print(f"Ratio (window/lifetime):          {ratio:.3f}x")
print()
if ratio > 1.05:
    print("ALERT: Our window vol > lifetime vol! Possible duplicates or vol is not lifetime.")
elif ratio > 0.8:
    print("INFO: Window ≈ lifetime -> ohanism started trading near our window start.")
else:
    print(f"INFO: Window = {ratio*100:.0f}% of lifetime -> meaningful history before our window.")

# 3. Per-date volume (to see when ohanism started)
fills_w2 = fills_w.with_columns(
    pl.from_epoch(pl.col("t_block_ns") // 1_000_000_000, time_unit="s")
      .dt.date().alias("date")
)
per_date = (fills_w2.group_by("date")
            .agg(pl.col("usdc_size").sum().alias("vol"),
                 pl.len().alias("n_fills"))
            .sort("date"))
print("Per-date volume:")
for r in per_date.iter_rows(named=True):
    print(f"  {r['date']}  fills={r['n_fills']:>5}  vol={r['vol']:>10,.1f} USDC")

# 4. Print all columns to understand the schema
print("\nColumns:", fills.columns[:30])
print("dtypes:", [(c, str(t)) for c, t in zip(fills.columns[:30], fills.dtypes[:30])])
