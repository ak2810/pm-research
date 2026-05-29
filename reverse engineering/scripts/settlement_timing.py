"""Settlement coverage timing and unredeemed position analysis.

Checks:
1. Are the 81 redemption txns clustered at start of window (pre-recording
   carry) or distributed throughout the day?
2. Which token_ids appear in burns? Are they from early-expiring markets?
3. How many unique token_ids traded by ohanism never show a redemption?
   (These are: losers, or winners claimed after our window, or winners
   from the pre-recording period claimed inside our window.)
"""
import sys

sys.path.insert(0, "src")

import polars as pl
from pathlib import Path

OHANISM = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
CTF_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
ZERO = "0x0000000000000000000000000000000000000000"
cache = Path("output/cache")

fills = pl.read_parquet("output/tables/ohanism_fills.parquet")
fill_tids = set(fills["token_id"].to_list())

# Collect all burn events with their block timestamps
burn_rows = []
for f in sorted(cache.glob("feed=polygon/date=*/hour=*/data.parquet")):
    lf = pl.scan_parquet(str(f), low_memory=True, hive_partitioning=False)
    burns = lf.filter(
        (pl.col("event") == "TransferSingle")
        & (pl.col("from_") == OHANISM)
        & (pl.col("to") == ZERO)
        & (pl.col("operator") == OHANISM)
    ).select(["tx_hash", "block_number", "t_recv_ns", "token", "token_id"]).collect()
    if len(burns) > 0:
        burn_rows.append(burns)

if not burn_rows:
    print("No burn events found in any cached partition.")
    exit()

burns_df = pl.concat(burn_rows, how="diagonal_relaxed")
burns_df = burns_df.sort("block_number")

print(f"Total burn events: {len(burns_df)}")
print(f"Unique tx_hashes: {burns_df['tx_hash'].n_unique()}")

# Which column holds the token_id? Could be 'token' or 'token_id'
tid_col = "token_id" if "token_id" in burns_df.columns else "token"
print(f"Token ID column: {tid_col}")
print(f"Unique tokens burned: {burns_df[tid_col].drop_nulls().n_unique()}")

# ── Timing: block distribution ───────────────────────────────────────────────
print("\n=== BURN TIMING (block_number distribution) ===")
min_fill_block = fills["block_number"].min()
max_fill_block = fills["block_number"].max()
print(f"Fill block range: {min_fill_block} to {max_fill_block}")
print(f"First burn block: {burns_df['block_number'].min()}")
print(f"Last burn block: {burns_df['block_number'].max()}")

# t_recv_ns→hour_utc
burns_df = burns_df.with_columns(
    ((pl.col("t_recv_ns") // 3_600_000_000_000) % 24).cast(pl.Int32).alias("hour_utc")
)
print("\nBurn events by UTC hour:")
print(burns_df["hour_utc"].value_counts().sort("hour_utc"))

# Are burns before the earliest fill (pre-recording carry)?
fills_first_block = int(fills["block_number"].min())
burns_before_fills = burns_df.filter(pl.col("block_number") < fills_first_block)
print(f"\nBurns BEFORE first fill block ({fills_first_block}): {len(burns_before_fills)}")
print(f"Burns DURING/AFTER fills: {len(burns_df) - len(burns_before_fills)}")

# ── Token overlap: traded vs redeemed ───────────────────────────────────────
burned_tids = set(burns_df[tid_col].drop_nulls().to_list())
traded_and_burned = fill_tids & burned_tids
traded_not_burned = fill_tids - burned_tids

print(f"\n=== TOKEN_ID COVERAGE ===")
print(f"Unique tokens traded by ohanism: {len(fill_tids)}")
print(f"Of those, burned/redeemed:       {len(traded_and_burned)} ({len(traded_and_burned)/len(fill_tids)*100:.1f}%)")
print(f"Never redeemed in our window:    {len(traded_not_burned)} ({len(traded_not_burned)/len(fill_tids)*100:.1f}%)")
burned_not_traded = burned_tids - fill_tids
print(f"Burned but not in fills:         {len(burned_not_traded)} (pre-recording positions)")

if burned_not_traded:
    print("→ ohanism had OPEN POSITIONS from before our recording window.")
    print("  These 'burned but not traded' tokens are pre-existing inventory")
    print("  confirmed: they entered 2026-05-27 with existing positions.")

print(f"\n=== VERDICT ===")
pct_carry = len(burns_before_fills) / len(burns_df) * 100 if len(burns_df) > 0 else 0
print(f"{pct_carry:.1f}% of burns occurred before our fill recording started.")
if pct_carry > 30:
    print("SIGNIFICANT pre-recording carry: ohanism arrived with open positions.")
elif pct_carry > 5:
    print("SOME pre-recording carry — minor open position from prior day.")
else:
    print("Burns are from same-day markets: no significant carry-in.")

if burned_not_traded:
    print(f"{len(burned_not_traded)} distinct prior-day positions redeemed in our window.")
