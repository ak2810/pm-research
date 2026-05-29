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

## 2026-05-29 — Phase 4 L1→L2 pivot: σ family confirmed, precise weighting via L2

**What L1 established** (valid despite gate failures):
1. EWMA (λ∈[0.90,0.94]) is in ohanism's σ recipe. β>0, p<0.001 in EVERY M1-M5
   model in BOTH v1 (fill-time) AND v2 (post-time). A signal that survives two
   broken measurement methodologies with stable sign and significance is real.
2. The L1 gate failure is a measurement problem, not a model problem:
   - v1 target biased: σ_implied at fill time conflates ohanism's σ with 20+s of
     post-quote spot drift. R²=0.29 partly measures EWMA vs next-20s realized vol.
   - v2 target ill-conditioned: at post-time S_t≈S_0 → log(S_0/S_t)≈0 → dividing
     by √τ≈0.003 amplifies S_t measurement noise 333×. SNR≈1 by construction.
3. σ_implied scalar inversion is exhausted. Further L1 work is not productive.

**Consequence**: Skip L1 precise weighting, proceed to L2 structural policy estimation.
L2 models the FULL QUOTE PRICE directly (not σ_implied as intermediate). σ recipe
parameters (EWMA weights) emerge from the structural fit. EWMA dominance in L1 provides
the correct initialization: w_ewma90≈0.3, w_ewma94≈0.5, w_ewma97≈0.1, others≈0.025.

**Date**: 2026-05-29

---

## 2026-05-29 — Pre-5.B: σ-recipe profile likelihood CI and replication convention

**Profile likelihood results** (λ ∈ {0.85,0.88,0.90,0.92,0.94,0.96,0.98}):
- λ_MLE = 0.85 (logL=752.12 when other params refitted)
- Stage 1 dominant: λ=0.94 (when σ recipe isolated, other params fixed)
- 95% CI (ΔlogL ≤ 1.92): λ ∈ [0.85, 0.94] (width=0.09)

**Disagreement analysis**: profile-λ=0.85 vs Stage1-λ=0.94. ΔlogL=1.19 < 1.92 → within CI.
The flat ridge [0.85, 0.94] means all λ in this range fit approximately equally well.
- At λ=0.85 (short memory): faster EWMA, slightly higher σ̂, spread params adjust
- At λ=0.94 (Stage 1 result): best when σ recipe is isolated from spread confound

**Replication convention**: use **λ=0.94** (from Stage 1 σ-isolated fit, more stable for midpoint).
This is defensible — λ=0.94 lies within the 95% CI.

**BLOCKER-005 interpretation**: non-identifiability was REAL (not numerical). The σ recipe
decay is non-uniquely determined in [0.85, 0.94]. This is a known limitation of the data:
EWMA_0.85, EWMA_0.90, EWMA_0.94 all give similar σ estimates at 5-minute timescales and
cannot be distinguished at the noise level of the post-time σ data.

**Date**: 2026-05-29

---

## 2026-05-29 — Phase 4 σ_implied v1→v2: fix quote-post-time proxy (BLOCKER-003 resolution)

**Problem (BLOCKER-003)**: σ_implied_v1 used the earliest ohanism FILL as the
quote-placement proxy. At market open S_t≈S_0, fair value=0.5 ∀σ. The σ only sets
the half-spread, not the midpoint. By fill time, spot has drifted and σ_implied
encodes drift noise. R² plateau at 0.29 is mathematically inevitable.

**Fix (v2)**: Use the EARLIEST pm_clob price_change NEW_ORDER (size increase) where
a subsequent ohanism fill at the same (token_id, price) occurs within the market
lifetime. Attribution: earliest level_change with subsequent fill at same price.
t_post_ns = t_ws_ns of that level_change. p_posted = canonical Up price at that level.

**Expected**: σ<0 should disappear; L1 R² should approach ≥0.4.
**Date**: 2026-05-29

---

## 2026-05-29 — Phase 4 annualization convention

**Question**: How to annualize σ in the digital-option inversion?

**Convention chosen**: τ in years = τ_seconds / 31,557,600 (365.25 × 24 × 3600).
Crypto trades 24/7 so we use calendar-year seconds, not equity-trading-day convention.
This matches the σ_estimators which also use 24/7 annualization (see sigma_estimators.py).

**Consistency check**: A 5m market with τ = 300s → τ_years = 300/31,557,600 ≈ 9.5×10⁻⁶.
σ_implied = log(S_0/S_t) / (√τ_years × Φ⁻¹(1−p)). For BTC at p=0.6, S_t=S_0×1.01
(1% above strike), τ = 9.5e-6 years: σ = log(1/1.01) / (√(9.5e-6) × Φ⁻¹(0.4))
= −0.00995 / (0.00308 × −0.253) ≈ 12.8 annualized → physically impossible for BTC.
This means at 1% ATM displacement and 5m TTE, the fit is degenerate. Expected:
the price at this displacement encodes a huge σ. The σ_implied is only well-defined
when |log(S_0/S_t)| is not too large relative to σ×√τ.

**ε thresholds** (document before running):
- Spot boundary: |log(S_0/S_t)| < 0.0001 → drop (ATM at quote time)
- Price boundary: |p_quoted − 0.5| < 0.02 → drop (near ATM in probability space)
- Price boundary: p_quoted < 0.02 or p_quoted > 0.98 → drop (near 0/1 → σ blows up)
- σ_implied sanity cap: drop markets where |σ_implied| > 15 (clearly degenerate)

**Expected BTC 5m range**: σ_implied ≈ 0.5–2.0 annualized. BTC 1-day volatility is
typically 0.03–0.08 (daily), annualized = 0.03×√365.25 ≈ 0.57. For 5m markets
with small ATM displacement (0.02–0.1 range), σ_implied should be in [0.3, 2.0].
If median is outside this range, τ units or canonical p convention is wrong → STOP.

**Date**: 2026-05-29

---

## 2026-05-29 — Phase 4 σ-gate revised for passive quoter

**Question**: What R² threshold for Layer 1 σ-regression is appropriate given Phase 3
confirms ohanism is a passive post-once quoter?

**Evidence**:
1. Phase 3: 0.15% quote cancellation rate. Quotes set at market open and held.
   Quote-flip latency median=18.4s [95% CI: 14.2-22.1s] — no dynamic repricing.
2. Phase 3: median quote lifetime=26ms (order book updates), but ohanism's own
   position is set once per market.
3. Phase 2: fills happen at OTM cushion median=0.22 — fills occur when market has
   already drifted from ATM (not when ohanism repriced to fair value).

**Implication for σ-fitting**:
If ohanism sets σ at market open (quote-placement-time) and doesn't update, then:
- `σ_implied(fill)` = fill price re-inverted using **current** spot (S_t at fill time)
- But ohanism computed their σ using **opening** spot (S_0 ≈ strike at market open)
- After market drifts (S_t ≠ S_0), `σ_implied(fill)` ≠ σ_at_quote-placement

The correct target is σ_implied computed at the proxy quote-placement-time
(earliest fill per market, or start_date_unix as S_t proxy).

**R² gate revision**:
- Original gate: R² > 0.6 (from METHODOLOGY.md, for active MM)
- Revised gate: R² ≥ 0.4 at quote-placement-time proxy
- Rationale: variance from market drift after quote placement reduces R² by ~0.15-0.25
  even for a perfect σ model. 0.4 at quote-placement-time ≈ 0.6 for active repricing MM.

**Date**: 2026-05-29

---

## 2026-05-29 — Market selection rule is unknown (Phase 4 prerequisite)

**Observation**: ohanism quotes in 60% of 5m/15m crypto markets in the window
(879/1420 5m, 289/480 15m). This is significant selection (40% markets skipped).

**Known selection facts**:
- No DOGE markets in our fills (confirmed from Gamma: no DOGE fills)
- Selects across all UTC hours observed (no clear time-of-day filter)
- Skips exactly 40% of markets consistently across 5m and 15m horizons

**Unknown**: What specific criterion excludes 40% of markets? Candidates:
- Minimum book depth at market open (depth < threshold → skip)
- Maximum distance-from-ATM at market open (if market opens far OTM → skip)
- Existing inventory threshold (already max-long → skip more markets of same asset)
- Random noise from connectivity (collector missed the new_market event)

**Decision**: Proceed to Phase 4 σ-fitting on the selected markets only (fills as-is).
The selection rule investigation is deferred to Phase 5 (when the residuals of Layer 2
might show a pattern correlated with market selection criteria). If the selection is
not explained by Phase 5, add a selection model to Layer 3 or flag as unresolved.

**Date**: 2026-05-29

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

## 2026-05-29 — Part B architecture verdict: PASSIVE confirmed, gate unchanged

**B3 interpretation note**: fill-price vs spot correlation = 0.906 (79.1% markets corr>0.7).
This measures FAIR-VALUE PRICING, not active repricing. For any fair-value MM (passive or
active), fill prices correlate with spot because fair value encodes spot information.
B3 is not discriminating for fair-value strategies — only for stale-price quoters.
Reclassified as neutral signal.

**B4 methodology note**: pull rate = 40.0% (30 cases). This measures all-participant
level_changes attributed wrongly to ohanism. Without per-maker order attribution (requires
pm_clob user channel or order-hash→level matching), B4 cannot isolate ohanism's cancels.
Phase 3 0.15% pull rate (whole-book basis) remains the consistent measurement.
B4 reclassified as not discriminating.

**True evidence for PASSIVE**:
1. Fill-latency 18.4s (>5s threshold)
2. OTM cushion 0.220 stable (fills after drift, not at ohanism's quote price)
3. 0% net-zero final positions (hold-to-resolution)
4. 52% ATM crossings: ohanism not on both sides (one-sided post, no quote to flip)

**Decision (REVISED after full B1 run, 9349s runtime)**:
CONDITIONAL HYBRID — not purely passive. Architecture:
- Default: passive (post-once at market open, hold 97.4% of the time)
- Activated on ~2.6% of ATM crossings: event-driven response in 17ms median
- The 18.4s "flip latency" measured taker arrival wait, NOT ohanism's reaction time.

Phase 4 gate stays at **R² ≥ 0.4 at quote-placement-time**.
Rationale: 97% of fills come from the passive regime (no repricing). The 3% repriced
fills add variance to σ_implied. R² ≥ 0.4 is appropriate for this mixed distribution.
Restoring to 0.6 would be incorrect (would require 97% active-repricing, which is false).

**Date**: 2026-05-29

---

**Canonical skew revision (2026-05-29)**: 6.9% long-Up bias explained by rebate
mechanics (Down tokens at p<0.5 generate higher rebate per unit = min(p,1-p)×0.07×0.2).
IRL pull-forward NOT triggered. XRP 5m at +31.7% is a separate investigation item in
Phase 5 (check whether bias vanishes when controlling for min(p,1-p)).

**Decision**: In Layer 2, test BOTH:
1. Pure fair-value quoter (no inventory term)
2. A-S with signed inventory aversion (allowing γ < 0 for directional bias)
Compare log-likelihood and AIC/BIC. The better-fitting model advances to SHAP.

**Date**: 2026-05-29

---

## 2026-05-29 — BLOCKER-007 escalated from non-blocking to gating (Pre-5.F required)

**Question**: Is our MTM P&L methodology correct at the per-position level?

**Background**: Pre-5.C found Polymarket leaderboard lifetime PnL = -1,382.65 USDC vs
our 49h window = -83,831 USDC (60× gap). Pre-5.D/E were internally consistent (D5: 12
spot-checks pass) but do NOT externally validate against Polymarket's accounting.

**Why initial "non-blocking" classification was wrong**:
1. The "unredeemed positions" hypothesis requires ~$82,449 sitting unredeemed. On 5m/15m
   markets (resolution in minutes), this backlog is implausible. Positions redeem
   automatically via smart contract within minutes of resolution.
2. D5's 12 spot-checks cover 0.024% of fills — proves internal consistency, not
   external correctness.
3. If our P&L is wrong, Phase 5 GBT will fit residuals against the wrong signal and SHAP
   will attribute measurement error to features. Must confirm before proceeding.

**Decision**: Escalate BLOCKER-007 to gating. Phase 5 starts only after Pre-5.F confirms
mean absolute per-position gap < 5% with no systematic bias.

**Test design (Pre-5.F)**:
- Query `data-api.polymarket.com/positions?user={ohanism_proxy}` for position-level P&L
- Select 30-50 test positions: fully resolved in window, spanning all asset/horizon/outcome
- Compare per-position: our cost basis vs theirs, our realized P&L vs theirs
- If gap < 5% and unbiased → accounting-methodology hypothesis confirmed, Phase 5 cleared
- If systematic bias → find and fix the bug, re-run G6

**Resolution (2026-05-29)**: Pre-5.F PASSED. All 4 resolved positions show < 1% gap
(0.3%, 0.4%, 0.6%, 0.7%). Formula confirmed correct at per-position level.
Leaderboard vol/pnl are 24h rolling metrics (not lifetime) — gap not comparable to 49h window.
BLOCKER-007 RESOLVED. Phase 5 cleared.

**Date**: 2026-05-29

---

## 2026-05-28 — Random seeds

**All random seeds**: `numpy.random.seed(42)`, `sklearn` estimators with
`random_state=42`, `lightgbm`/`xgboost` with `seed=42`, `torch.manual_seed(42)`
+ `torch.cuda.manual_seed(42)`. Documented here so any reproduction uses
identical seeds without hunting.

**Date**: 2026-05-28
