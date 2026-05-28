# DECISIONS

Every non-obvious technical choice is recorded here with rationale,
alternatives considered, and date.

---

## 2026-05-28 — PyTorch CUDA wheel selection

**Question**: Which PyTorch wheel to install for RTX 3060 / CUDA 13.1 driver?

**Options considered**:
1. `cu121` (CUDA 12.1) — stable, widely tested
2. `cu124` (CUDA 12.4) — latest stable as of Aug 2025
3. `cu126` (CUDA 12.6) — if available
4. CPU-only — unacceptable for Layer 4

**Decision**: Install `torch` from `https://download.pytorch.org/whl/cu124`.
CUDA drivers are backward-compatible: a CUDA 13.1 driver can run applications
compiled for CUDA 12.4. cu124 is the latest confirmed stable wheel at the time
of writing.

**Verified**: `torch.cuda.is_available()` == True; device = `NVIDIA GeForce RTX
3060`. Logged in RESULTS.md.

**Date**: 2026-05-28

---

## 2026-05-28 — LightGBM GPU support decision

**Question**: Use CPU or GPU LightGBM for Layer 3 (GBT residual model)?

**Options considered**:
1. Standard pip wheel — CPU-only on Windows (verified: standard wheel does not
   include OpenCL GPU support on Windows)
2. Compile from source with `-DUSE_GPU=ON` — requires Boost, CMake, MSVC;
   significant build complexity
3. CPU-only pip install — simpler; acceptable for dataset size

**Decision**: Install CPU-only LightGBM via pip. For Layer 3, the training
dataset is ~20k ohanism fills/day × N features — trivially small for CPU
LightGBM. GPU training would save <1 minute. Not worth the build complexity.
Document this as a known limitation: GPU acceleration not available for Layer 3
on this platform.

**Layer 3 GPU status**: CPU fallback, documented, acceptable.

**Date**: 2026-05-28

---

## 2026-05-28 — Python package layout

**Question**: Where to place the reverse-engineering Python package relative to
the parent `pm-research` repo?

**Options considered**:
1. Extend the parent `pm_research` package — risks coupling analysis code to
   collectors; mypy would merge namespaces
2. New package `reverse_engineering` under `reverse engineering/src/` — clean
   separation; own pyproject.toml, own tests, own deps
3. Flat scripts under `reverse engineering/` — no package structure; no mypy
   --strict possible

**Decision**: Option 2. `reverse_engineering` package with src layout, own
`pyproject.toml`. Installed in editable mode (`pip install -e .`) from within
`reverse engineering/`. Parent package unchanged.

**Date**: 2026-05-28

---

## 2026-05-28 — Local Parquet cache size cap

**Question**: What disk budget for `output/cache/` (synced S3 Parquet)?

**Context**: ~170M rows/day across feeds. At ~300 bytes/row uncompressed,
pm_clob alone is ~31 GB/day. Parquet compresses ~5-8×, so ~4-6 GB/day/feed.

**Decision**: Cap = 200 GB. Implemented in `io/s3_sync.py` — when cache exceeds
200 GB, evict oldest partitions (by date/hour directory mtime) and re-sync on
demand. This supports ~14-30 days of multi-feed data depending on compression.

**Date**: 2026-05-28

---

## 2026-05-28 — Decimal precision for monetary values

**Decision**: `decimal.Decimal` for ALL price, size, fee, position values.
Never `float`. Storage as string with 6 decimal places matching on-chain
precision. This matches the parent project convention (`*_raw` uint256 string +
`*_decimal` 6-dp string). Polars stores these as `Utf8`; parse to `Decimal` on
read, convert back to string for storage.

**Date**: 2026-05-28

---

## 2026-05-29 — Inventory behavior changes Layer 2 prior

**Observation**: 0% net-zero final positions across 1,651 markets. ohanism always
carries residual inventory to settlement. 83.4% SELL side (short-Up dominant).

**Implication for Layer 2 (structural ML)**:
- The A-S inventory aversion parameter γ likely encodes a DIRECTIONAL BIAS, not
  just risk aversion. ohanism may have γ < 0 on the Up token (prefers being short).
- Alternative: γ is small and the strategy is more "take-what-comes" than active
  inventory management.
- The Layer 2 likelihood must distinguish between (a) inventory aversion = 0 (no
  skewing, just quoting fair value) and (b) directional bias (systematic short-Up
  quote skew).

**Decision**: In Layer 2, test BOTH:
1. Pure fair-value quoter (no inventory term)
2. A-S with signed inventory aversion (allowing γ < 0 for directional bias)
Compare log-likelihood and AIC/BIC. The better-fitting model advances to SHAP.

**Date**: 2026-05-29

---

## 2026-05-28 — Random seeds

**All random seeds**: `numpy.random.seed(42)`, `sklearn` estimators with
`random_state=42`, `lightgbm`/`xgboost` with `seed=42`, `torch.manual_seed(42)`
+ `torch.cuda.manual_seed(42)`. Documented here so any reproduction uses
identical seeds without hunting.

**Date**: 2026-05-28
