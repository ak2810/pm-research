"""Check for (block_number, log_index) duplicates in ohanism_fills_full.parquet."""
import sys
sys.path.insert(0, "src")
import polars as pl
from reverse_engineering.config import get_settings

cfg = get_settings()
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills_full.parquet"))

n_total = len(fills)
n_unique_blk_log = fills.select(["block_number","log_index"]).n_unique()
n_unique_tx_log  = fills.select(["tx_hash","log_index"]).n_unique()

print(f"Total rows:                      {n_total:>8}")
print(f"Unique (block_number,log_index): {n_unique_blk_log:>8}  dup_rate={1-n_unique_blk_log/n_total:.3f}")
print(f"Unique (tx_hash,log_index):      {n_unique_tx_log:>8}  dup_rate={1-n_unique_tx_log/n_total:.3f}")
print(f"Unique order_hash:               {fills['order_hash'].n_unique():>8}  (expected lower: multiple fills per order)")

# Sample a duplicate (block_number,log_index) pair
dups = (fills.group_by(["block_number","log_index"])
        .agg(pl.len().alias("cnt"))
        .filter(pl.col("cnt") > 1)
        .sort("cnt", descending=True))
print(f"\nDuplicate (block_number,log_index) pairs: {len(dups)}")
if len(dups) > 0:
    print(f"Max count for a single pair: {int(dups['cnt'].max())}")
    # Show a sample duplicate
    top = dups.head(1)
    bn, li = int(top["block_number"][0]), int(top["log_index"][0])
    ex = fills.filter((pl.col("block_number")==bn) & (pl.col("log_index")==li))
    print(f"Sample duplicate rows (block={bn}, log_idx={li}):")
    for r in ex.head(3).iter_rows(named=True):
        print(f"  tx_hash={r['tx_hash'][:20]}  order_hash={r['order_hash'][:20]}  "
              f"t_recv_ns={r['t_recv_ns']}  is_backfilled={r['is_backfilled']}  "
              f"price={r['price']}  size={r['size']}")

# How many rows would survive deduplication?
deduped = fills.unique(subset=["block_number","log_index"], keep="first")
print(f"\nAfter dedup: {len(deduped)} rows ({len(fills)-len(deduped)} removed)")

# Compute MTM impact of duplicates
fills_w = fills.with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
]).with_columns(
    (pl.col("price_f") * pl.col("size_f")).alias("usdc_size")
)
deduped_w = deduped.with_columns([
    pl.col("price").cast(pl.Float64).alias("price_f"),
    pl.col("size").cast(pl.Float64).alias("size_f"),
]).with_columns(
    (pl.col("price_f") * pl.col("size_f")).alias("usdc_size")
)

print(f"\nVolume before dedup: {float(fills_w['usdc_size'].sum()):>12,.2f} USDC")
print(f"Volume after dedup:  {float(deduped_w['usdc_size'].sum()):>12,.2f} USDC")
print(f"Leaderboard vol:         270,158.65 USDC")
print(f"Ratio after dedup / leaderboard: {float(deduped_w['usdc_size'].sum())/270158.65:.3f}x")
