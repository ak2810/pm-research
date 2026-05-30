"""V5: pm_clob market subscription coverage for bot2855 vs ohanism."""
import sys; sys.path.insert(0, 'src')
import polars as pl
from reverse_engineering.config import get_settings
from reverse_engineering.io.catalog import list_local_partitions
from reverse_engineering.io.local_reader import scan_feed

BOT2855 = "0x2855555a48ee7ec2e67272701651bfe77034ebe8"
OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
cfg = get_settings()

# Use last 24h of local data
parts = sorted(list_local_partitions('polygon'), key=lambda p: (p.date, p.hour))
last24_parts = parts[-24:]
print(f"Checking last {len(last24_parts)} polygon partitions for coverage")

# Step 1: Collect bot2855 and ohanism token_ids from polygon fills
bot_tokens = set()
oha_tokens = set()
for p in last24_parts:
    lf = scan_feed('polygon', p.date, p.hour)
    df = (lf.filter(
        (pl.col("event") == "OrderFilled")
        & ((pl.col("maker").is_in([BOT2855, OHANISM])) | (pl.col("taker").is_in([BOT2855, OHANISM])))
    ).select(["maker","taker","token_id"]).collect())
    for row in df.iter_rows(named=True):
        tid = row["token_id"]
        if row["maker"] == BOT2855 or row["taker"] == BOT2855:
            bot_tokens.add(tid)
        if row["maker"] == OHANISM or row["taker"] == OHANISM:
            oha_tokens.add(tid)

print(f"bot2855 unique token_ids in last 24h: {len(bot_tokens)}")
print(f"ohanism  unique token_ids in last 24h: {len(oha_tokens)}")

# Step 2: Collect token_ids that appear in pm_clob book events
clob_parts = sorted(list_local_partitions('pm_clob'), key=lambda p: (p.date, p.hour))
last24_clob = clob_parts[-24:]
clob_tokens = set()
for p in last24_clob:
    lf = scan_feed('pm_clob', p.date, p.hour)
    df = (lf.filter(
        (pl.col("event_type").is_in(["book","new_order"])) & pl.col("asset_id").is_not_null()
    ).select(["asset_id"]).collect())
    for tid in df["asset_id"].to_list():
        clob_tokens.add(str(tid))

print(f"pm_clob token_ids covered (last 24h): {len(clob_tokens)}")
print()

# Step 3: Compute coverage
bot_covered = len(bot_tokens & clob_tokens)
bot_total   = len(bot_tokens)
oha_covered = len(oha_tokens & clob_tokens)
oha_total   = len(oha_tokens)

bot_coverage = bot_covered / bot_total if bot_total else 0
oha_coverage = oha_covered / oha_total if oha_total else 0

print(f"bot2855 pm_clob coverage: {bot_covered}/{bot_total} = {bot_coverage*100:.1f}%")
print(f"ohanism  pm_clob coverage: {oha_covered}/{oha_total} = {oha_coverage*100:.1f}%")
print()

gate = "PASS ✓" if bot_coverage >= 0.80 else "FAIL <80%"
note = "" if bot_coverage >= 0.80 else "  LIMITATION: Phase 3 work constrained to covered subset"
print(f"V5 {gate}{note}")

# Coverage gap details
bot_uncovered = bot_tokens - clob_tokens
print(f"\nbot2855 uncovered token_ids: {len(bot_uncovered)} (first 5: {list(bot_uncovered)[:5]})")
