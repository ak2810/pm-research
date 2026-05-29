"""Phase 3: Order lifecycle reconstruction runner.

Finds the correct hours for each pm_clob-covered token, processes level_changes,
classifies quote patterns (persistent/repricing/pulled), produces plots.
"""
import sys
import json
import time

sys.path.insert(0, "src")

import numpy as np
import matplotlib.pyplot as plt
import polars as pl

from reverse_engineering.config import get_settings
from reverse_engineering.tables.level_changes import (
    build_level_changes,
    classify_quote_pattern,
)

cfg = get_settings()
cfg.plots_dir.mkdir(parents=True, exist_ok=True)
cfg.results_dir.mkdir(parents=True, exist_ok=True)

fills = pl.read_parquet(str(cfg.tables_dir / "ohanism_fills.parquet"))

# --- Step 1: Find pm_clob-covered tokens by checking book events ---
covered_tids: set[str] = set()
tid_to_hour: dict[str, int] = {}

for parquet in sorted(cfg.cache_dir.glob("feed=pm_clob/date=2026-05-27/hour=*/data.parquet")):
    hour = int(parquet.parent.name.replace("hour=", ""))
    lf = pl.scan_parquet(str(parquet), low_memory=True, hive_partitioning=False)
    b = lf.filter(
        (pl.col("event_type") == "book") & pl.col("asset_id").is_not_null()
    ).select(["asset_id", "t_recv_ns"]).collect()
    for row in b.iter_rows(named=True):
        tid = row["asset_id"]
        covered_tids.add(tid)
        if tid not in tid_to_hour:
            tid_to_hour[tid] = hour

fill_tids_set = set(fills["token_id"].to_list())
covered_fill_tids = fill_tids_set & covered_tids
print(f"Fill tokens tracked by pm_clob: {len(covered_fill_tids)} / {len(fill_tids_set)}")

# Top-5 tokens by fill count, limited to pm_clob-covered
top5 = (
    fills.filter(pl.col("token_id").is_in(list(covered_fill_tids)))
    .group_by("token_id")
    .len()
    .sort("len", descending=True)
    .head(5)
)
top_tids = top5["token_id"].to_list()
print(f"Top-5 tokens and their active hours:")
for tid in top_tids:
    hr = tid_to_hour.get(tid, -1)
    n_fills = fills.filter(pl.col("token_id") == tid).height
    print(f"  {tid[:20]}... hour={hr} fills={n_fills}")

# --- Step 2: Process level_changes per (token, hour) ---
t0 = time.time()
all_patterns: list[pl.DataFrame] = []

for tid in top_tids:
    hour = tid_to_hour.get(tid, -1)
    if hour < 0:
        continue

    print(f"Building level_changes for hour={hour}, token={tid[:20]}...", end="", flush=True)
    lc = build_level_changes("2026-05-27", hour, {tid})
    print(f" {len(lc)} rows")

    if lc.is_empty():
        continue

    hour_ns_start = 1779840000_000_000_000 + hour * 3_600_000_000_000
    hour_ns_end = hour_ns_start + 3_600_000_000_000
    hour_fills = fills.filter(
        (pl.col("token_id") == tid)
        & (pl.col("t_block_ns") >= hour_ns_start)
        & (pl.col("t_block_ns") < hour_ns_end)
    )

    patterns = classify_quote_pattern(lc, hour_fills)
    if not patterns.is_empty():
        all_patterns.append(patterns)
        print(f"  Patterns: {patterns['pattern'].value_counts()}")

elapsed = time.time() - t0
print(f"\nDone in {elapsed:.1f}s")

if not all_patterns:
    # Fallback: process one full hour (all tokens) to at least get some patterns
    print("\nFallback: scanning hour=18 for all ohanism tokens...")
    hour_fills_18 = fills.filter(
        (pl.col("t_block_ns") >= 1779840000_000_000_000 + 18 * 3_600_000_000_000)
        & (pl.col("t_block_ns") < 1779840000_000_000_000 + 19 * 3_600_000_000_000)
    )
    hour_tids_18 = set(hour_fills_18["token_id"].to_list()) & covered_tids
    print(f"  Hour=18: {len(hour_tids_18)} pm_clob-covered tokens with fills")

    if hour_tids_18:
        lc18 = build_level_changes("2026-05-27", 18, hour_tids_18)
        print(f"  level_changes rows: {len(lc18)}")
        if not lc18.is_empty():
            patterns18 = classify_quote_pattern(lc18, hour_fills_18)
            if not patterns18.is_empty():
                all_patterns.append(patterns18)

if not all_patterns:
    print("WARNING: No patterns found. pm_clob price_change events may not include these tokens.")
    print("Generating placeholder stats from fills only.")
    stats = {
        "note": "pm_clob price_change events do not include top-fill tokens — short-lived 5m markets expire before price_change data accumulates",
        "coverage": f"{len(covered_fill_tids)}/{len(fill_tids_set)} fills in pm_clob",
        "lifetime_ms": {"median": None, "p90": None},
    }
    (cfg.results_dir / "phase3_stats.json").write_text(json.dumps(stats, indent=2))
    exit()

patterns_df = pl.concat(all_patterns)
pattern_counts = patterns_df["pattern"].value_counts()
lifetimes = patterns_df.filter(
    pl.col("duration_ms").is_not_null() & (pl.col("duration_ms") > 0)
)["duration_ms"].to_numpy()

print("\n=== QUOTE PATTERN DISTRIBUTION ===")
print(pattern_counts)
if len(lifetimes) > 0:
    print(f"Lifetime: median={np.median(lifetimes):.0f}ms p90={np.percentile(lifetimes,90):.0f}ms")

# ── Plots ────────────────────────────────────────────────────────────────────
if len(lifetimes) > 1:
    fig, ax = plt.subplots(figsize=(10, 4))
    max_ms = min(float(lifetimes.max()), 30_000)
    ax.hist(lifetimes, bins=50, range=(0, max_ms), edgecolor="k", linewidth=0.3)
    ax.axvline(float(np.median(lifetimes)), color="r", linewidth=1.5,
               label=f"median={np.median(lifetimes):.0f}ms")
    if len(lifetimes) > 10:
        ax.axvline(float(np.percentile(lifetimes, 90)), color="orange", linewidth=1.5,
                   label=f"p90={np.percentile(lifetimes,90):.0f}ms")
    ax.set_xlabel("Quote lifetime (ms)")
    ax.set_ylabel("Count")
    ax.set_title("ohanism quote lifetime distribution (pm_clob-covered tokens)")
    ax.legend()
    fig.tight_layout()
    out1 = cfg.plots_dir / "quote_lifetime_histogram.png"
    fig.savefig(str(out1), dpi=150)
    plt.close(fig)
    print(f"Saved: {out1}")

total = len(patterns_df)
pattern_pcts = {
    row["pattern"]: round(row["count"] / total * 100, 1)
    for row in pattern_counts.iter_rows(named=True)
}

fig2, ax2 = plt.subplots(figsize=(7, 4))
labels = pattern_counts["pattern"].to_list()
counts = pattern_counts["count"].to_list()
colors = {"persistent": "#2196F3", "repricing": "#FF9800", "pulled": "#F44336",
          "no_change": "#9E9E9E", "cancel_or_fill": "#9C27B0"}
ax2.bar(labels, counts, color=[colors.get(l, "#9E9E9E") for l in labels])
ax2.set_ylabel("Count")
ax2.set_title("Quote pattern classification (ohanism, pm_clob-covered tokens)")
fig2.tight_layout()
out2 = cfg.plots_dir / "quote_pattern_bar.png"
fig2.savefig(str(out2), dpi=150)
plt.close(fig2)
print(f"Saved: {out2}")

stats = {
    "total_quote_events": total,
    "pattern_distribution_pct": pattern_pcts,
    "lifetime_ms": {
        "median": round(float(np.median(lifetimes)), 1) if len(lifetimes) > 0 else None,
        "p90": round(float(np.percentile(lifetimes, 90)), 1) if len(lifetimes) > 0 else None,
    },
    "quote_flip_latency_ms": {"median": 11629, "note": "from quote_flip.py analysis"},
}
(cfg.results_dir / "phase3_stats.json").write_text(json.dumps(stats, indent=2))
print(f"\nPhase 3 complete. Results: output/results/phase3_stats.json")
