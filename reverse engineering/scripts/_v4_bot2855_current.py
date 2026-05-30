"""V4: Verify bot2855 fills appear in most recent polygon partition."""
import sys; sys.path.insert(0, 'src')
import polars as pl
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_s3_partitions, Partition
from reverse_engineering.io.s3_sync import download_partition

BOT2855 = "0x2855555a48ee7ec2e67272701651bfe77034ebe8"
OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
cfg = get_settings()

# Get most recent polygon S3 partition
parts = sorted(list_s3_partitions('polygon'), key=lambda p: (p.date, p.hour))
latest = parts[-1]
print(f"Most recent polygon S3 partition: {latest.date} h{latest.hour:02d}")

# Ensure it's cached locally
local_path = cfg.cache_dir / f"feed=polygon/date={latest.date}/hour={latest.hour:02d}/data.parquet"
if not local_path.exists():
    print("Downloading latest partition...")
    download_partition(latest, overwrite=False)

# Scan for bot2855 and ohanism fills
lf = pl.scan_parquet(str(local_path), low_memory=True, hive_partitioning=False,
                     use_statistics=False)
df = lf.filter(
    (pl.col("event") == "OrderFilled")
).select(["maker","taker","block_number","log_index"]).collect()

bot_fills = df.filter(
    (pl.col("maker") == BOT2855) | (pl.col("taker") == BOT2855)
)
oha_fills = df.filter(
    (pl.col("maker") == OHANISM) | (pl.col("taker") == OHANISM)
)

print(f"\nTotal OrderFilled in partition: {len(df)}")
print(f"bot2855 fills: {len(bot_fills)}")
print(f"ohanism fills:  {len(oha_fills)}")
print()

if len(bot_fills) > 0:
    print(f"V4 PASS: bot2855 has {len(bot_fills)} fills in most recent partition")
    print(f"  Rate comparison — bot2855:{len(bot_fills)} ohanism:{len(oha_fills)} "
          f"(ratio {len(bot_fills)/max(len(oha_fills),1):.2f}x)")
else:
    print("V4 CONCERN: bot2855 has 0 fills in most recent partition")
    print("  Checking if bot2855 was active recently (past 3 partitions)...")
    for p in parts[-3:]:
        lpath = cfg.cache_dir / f"feed=polygon/date={p.date}/hour={p.hour:02d}/data.parquet"
        if lpath.exists():
            lf2 = pl.scan_parquet(str(lpath), low_memory=True, hive_partitioning=False,
                                   use_statistics=False)
            n = len(lf2.filter(
                (pl.col("event") == "OrderFilled")
                & ((pl.col("maker") == BOT2855) | (pl.col("taker") == BOT2855))
            ).collect())
            print(f"  {p.date} h{p.hour:02d}: {n} fills")
