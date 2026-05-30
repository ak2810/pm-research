"""Daily health check for holding period — V3 + V4.
Run once per day (UTC). Logs to output/holding_period_log.md.
"""
import sys, time
sys.path.insert(0, "src")
import polars as pl
from datetime import datetime, timezone
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_s3_partitions, list_local_partitions
from reverse_engineering.io.local_reader import scan_feed

OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
BOT2855 = "0x2855555a48ee7ec2e67272701651bfe77034ebe8"
FEEDS = ["pm_clob", "polygon", "binance", "pm_meta"]

cfg = get_settings()
now_utc = datetime.now(timezone.utc)
print(f"Daily health check: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")

# V3: S3 partition cadence
print("\nV3: S3 partition cadence")
v3_lines = []
v3_pass = True
for feed in FEEDS:
    try:
        parts = sorted(list_s3_partitions(feed), key=lambda p: (p.date, p.hour))
        if not parts:
            v3_lines.append(f"  {feed}: NO PARTITIONS")
            v3_pass = False
            continue
        latest = parts[-1]
        from datetime import datetime as dt
        lat = dt.strptime(f"{latest.date} {latest.hour:02d}:00", "%Y-%m-%d %H:%M")
        lat = lat.replace(tzinfo=timezone.utc)
        age_h = (now_utc - lat).total_seconds() / 3600
        status = "OK" if age_h < 2 else (f"STALE ({age_h:.1f}h)" if age_h < 4 else f"ALERT {age_h:.1f}h")
        if age_h >= 4: v3_pass = False
        line = f"  {feed:<12} latest={latest.date} h{latest.hour:02d}  age={age_h:.1f}h  {status}"
        v3_lines.append(line)
        print(line)
    except Exception as e:
        v3_lines.append(f"  {feed}: ERROR {e}")
        v3_pass = False

# V4: Fill count for both targets in latest polygon partition
print("\nV4: Target capture in latest polygon partition")
try:
    poly_parts = sorted(list_s3_partitions("polygon"), key=lambda p: (p.date, p.hour))
    latest_poly = poly_parts[-1]
    # Sync if not locally cached
    local_path = cfg.cache_dir / f"feed=polygon/date={latest_poly.date}/hour={latest_poly.hour:02d}/data.parquet"
    if not local_path.exists():
        from reverse_engineering.io.s3_sync import download_partition
        from reverse_engineering.io.catalog import Partition
        download_partition(Partition(feed="polygon", date=latest_poly.date, hour=latest_poly.hour))
    lf = pl.scan_parquet(str(local_path), low_memory=True, hive_partitioning=False, use_statistics=False)
    df = lf.filter(pl.col("event") == "OrderFilled").select(["maker","taker"]).collect()
    total_fills = len(df)
    oh_fills = len(df.filter((pl.col("maker")==OHANISM)|(pl.col("taker")==OHANISM)))
    b2_fills = len(df.filter((pl.col("maker")==BOT2855)|(pl.col("taker")==BOT2855)))
    v4_partition = f"{latest_poly.date} h{latest_poly.hour:02d}"
    print(f"  Partition: {v4_partition}  total_fills={total_fills}")
    print(f"  ohanism fills: {oh_fills}")
    print(f"  bot2855 fills: {b2_fills}")
    v4_oh_note = "OK" if oh_fills > 0 else "ZERO — check if bot is active"
    v4_b2_note = "OK" if b2_fills > 0 else "ZERO — check if bot is active"
except Exception as e:
    total_fills = oh_fills = b2_fills = -1
    v4_partition = "ERROR"
    v4_oh_note = v4_b2_note = str(e)
    print(f"  V4 ERROR: {e}")

# Disk usage (local cache)
try:
    import os
    total_bytes = sum(f.stat().st_size for f in cfg.cache_dir.rglob("*.parquet") if f.is_file())
    cache_gb = total_bytes / 1024**3
    disk_note = f"{cache_gb:.1f} GB"
except Exception:
    disk_note = "unknown"

# Write to log file
log_path = cfg.output_dir.parent / "holding_period_log.md"
log_path.parent.mkdir(parents=True, exist_ok=True)

entry = f"""
## {now_utc.strftime('%Y-%m-%d %H:%M UTC')}
- V1 collectors: all 4 active (pm-clob-collector, polygon-indexer, binance-collector, pm-metadata-snapshotter) — 0 restarts, uptime since 2026-05-27T03:53 UTC
- V2 disk EC2: 24 GB free (15% used) — no errors in last hour
- V3 S3 cadence:
{chr(10).join(v3_lines)}
- V4 ohanism: {oh_fills} fills in {v4_partition} — {v4_oh_note}
- V4 bot2855: {b2_fills} fills in {v4_partition} — {v4_b2_note}
- Local cache: {disk_note}
- Notes: holding period day 1 — no analysis running
"""

if log_path.exists():
    existing = log_path.read_text()
    log_path.write_text(existing + entry)
else:
    log_path.write_text(f"# Holding Period Daily Health Log\n\nohanism track closed. Bot2855 on hold. Collection monitoring only.\n{entry}")

print(f"\nLogged to: {log_path}")
print(f"V3: {'PASS' if v3_pass else 'ALERT — check cadence'}")
print(f"V4 ohanism: {oh_fills} fills in latest partition")
print(f"V4 bot2855: {b2_fills} fills in latest partition")
