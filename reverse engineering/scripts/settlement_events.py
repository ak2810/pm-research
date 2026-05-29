"""Check PositionsMerge and PayoutRedemption events for ohanism in polygon.

Determines what fraction of ohanism's positions close via:
(a) Merge (YES+NO → USDC, mid-market close)
(b) PayoutRedemption (winning token → USDC post-resolution)
vs (c) hold-to-resolution without claiming (position abandoned or
     claimed outside our window).

If ohanism merges frequently, the 0% net-zero finding in OrderFilled-only
reconstruction is an artifact — they ARE closing positions, just off the
CLOB. If they rarely merge and mainly redeem winners, they are truly
one-sided and hold-to-settle.
"""
import sys

sys.path.insert(0, "src")

import polars as pl
from pathlib import Path
from reverse_engineering.config import get_settings
from reverse_engineering.io.local_reader import scan_feed

cfg = get_settings()
cache = cfg.cache_dir

OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
CTF_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"

# Resolve signer EOA via ProxyCreated event (needed since CTF events index
# the EOA directly, not the proxy wallet in many cases)
# From VERIFIED_FACTS: factory 0xaB45c5A4B0c941a2F231C04C3f49182e1A254052
# ProxyCreated(address proxy, address signer)
# We know proxy = OHANISM; EOA = the signer

# First: check what events we have in polygon for CTF contract and ohanism
print("=== Searching polygon for CTF settlement events ===")
ctf_events_found = {}
merge_rows_all = []
redeem_rows_all = []

for f in sorted(cache.glob("feed=polygon/date=*/hour=*/data.parquet")):
    lf = pl.scan_parquet(str(f), low_memory=True, hive_partitioning=False)

    # Check which event types exist in this partition
    events = lf.select("event").collect()["event"].value_counts()
    ctf_events = events.filter(
        pl.col("event").is_in(["PositionsMerge", "PayoutRedemption",
                                "TransferSingle", "TransferBatch"])
    )
    if len(ctf_events) > 0:
        for row in ctf_events.iter_rows(named=True):
            ctf_events_found[row["event"]] = ctf_events_found.get(row["event"], 0) + row["count"]

    # Check for PositionsMerge where ohanism is stakeholder
    merge = lf.filter(
        (pl.col("event") == "PositionsMerge")
        & (
            (pl.col("from_") == OHANISM)     # some fields use from_
            | (pl.col("to") == OHANISM)
            | (pl.col("operator") == OHANISM)
            | (pl.col("token") == OHANISM)  # might not exist but safe
        )
    ).collect()

    # Also try via the 'token' or 'amount' columns
    # PositionsMerge has: stakeholder, collateralToken, parentCollectionId, conditionId
    # In the polygon schema, these map to from_/to/operator etc.
    if len(merge) > 0:
        merge_rows_all.append(merge)

    # Check for PayoutRedemption
    redeem = lf.filter(
        (pl.col("event") == "PayoutRedemption")
        & (
            (pl.col("from_") == OHANISM)
            | (pl.col("to") == OHANISM)
            | (pl.col("operator") == OHANISM)
        )
    ).collect()
    if len(redeem) > 0:
        redeem_rows_all.append(redeem)

print(f"CTF event types found: {ctf_events_found}")
print(f"PositionsMerge partitions with ohanism: {len(merge_rows_all)}")
print(f"PayoutRedemption partitions with ohanism: {len(redeem_rows_all)}")

total_merges = sum(len(df) for df in merge_rows_all)
total_redeems = sum(len(df) for df in redeem_rows_all)
print(f"Total PositionsMerge rows: {total_merges}")
print(f"Total PayoutRedemption rows: {total_redeems}")

if total_merges > 0:
    merges = pl.concat(merge_rows_all)
    print("Sample merge cols:", merges.columns[:10])
    print(merges.head(3))

if total_redeems > 0:
    redeems = pl.concat(redeem_rows_all)
    print("Sample redeem cols:", redeems.columns[:10])
    print(redeems.head(3))

# Compare to fill count
fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills.parquet"))
unique_markets_traded = fills["market"].drop_nulls().n_unique()
unique_tokens_traded = fills["token_id"].n_unique()
print(f"\nFills: {len(fills)} across {unique_tokens_traded} tokens, {unique_markets_traded} markets")
print(f"Merge events: {total_merges} (each closes one conditionId worth of Yes+No)")
print(f"Redeem events: {total_redeems} (each claims payout on one position)")

merge_rate = total_merges / unique_markets_traded if unique_markets_traded > 0 else 0
redeem_rate = total_redeems / unique_markets_traded if unique_markets_traded > 0 else 0
print(f"\nMerge rate: {merge_rate:.3f} merges per unique market traded")
print(f"Redeem rate: {redeem_rate:.3f} redeems per unique market traded")

if total_merges == 0 and total_redeems == 0:
    print("\nNOTE: Zero merge/redeem events found.")
    print("Either: (a) ohanism uses a different address for settlement,")
    print("        (b) settlement happens outside our recording window,")
    print("        (c) positions are held to resolution payout auto-credited,")
    print("        (d) polygon indexer doesn't record CTF internal events for this proxy.")
    print("The 0% net-zero finding stands pending verification via on-chain block explorer.")
elif total_merges > 0:
    frac_closed_via_merge = total_merges / (len(fills) / 2)  # rough: each merge closes ~2 fills
    print(f"\nEstimated fraction of positions closed via Merge: {frac_closed_via_merge:.1%}")
    if frac_closed_via_merge > 0.1:
        print("SIGNIFICANT: >10% positions close via Merge. 0% net-zero finding is artifact.")
    else:
        print("MINOR: <10% via Merge. 0% net-zero finding mostly holds.")
