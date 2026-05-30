"""V3: Check S3 partition recency for all 4 feeds."""
import sys; sys.path.insert(0, 'src')
from reverse_engineering.io.catalog import list_s3_partitions
from datetime import datetime, timezone, timedelta

feeds = ['pm_clob', 'polygon', 'binance', 'pm_meta']
now_utc = datetime.now(timezone.utc)
print(f"Current UTC: {now_utc.strftime('%Y-%m-%d %H:%M')}")
print()
stale = []
for feed in feeds:
    parts = sorted(list_s3_partitions(feed), key=lambda p: (p.date, p.hour))
    latest = parts[-1] if parts else None
    if latest:
        lat_dt = datetime.strptime(f"{latest.date} {latest.hour:02d}:00", '%Y-%m-%d %H:%M')
        lat_dt = lat_dt.replace(tzinfo=timezone.utc)
        age_h = (now_utc - lat_dt).total_seconds() / 3600
        status = 'PASS ✓' if age_h < 4 else 'STALE (>4h) ✗'
        print(f"  {feed:<22} latest={latest.date} h{latest.hour:02d}  age={age_h:.1f}h  {status}")
        if age_h >= 4:
            stale.append(feed)
    else:
        print(f"  {feed:<22} NO PARTITIONS")
        stale.append(feed)

print()
print(f"V3 {'PASS ✓' if not stale else 'FAIL — stale: ' + str(stale)}")
