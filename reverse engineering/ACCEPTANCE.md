# ACCEPTANCE GATES — Per-Phase Pass/Fail Criteria

> Place this file at `c:\users\avych\pm-research\reverse engineering\ACCEPTANCE.md`. The agent reads it at the end of every phase and verifies every box is checked before advancing.

---

## Phase 0 — Bootstrap

- [ ] `c:\users\avych\pm-research\reverse engineering\` exists with the exact folder structure from §4.1 of MASTER_PROMPT.md (including `output/cache/` for the local Parquet cache and `io/s3_sync.py`, `io/local_reader.py`, `io/ec2.py`).
- [ ] `METHODOLOGY.md` exists and matches §9 of MASTER_PROMPT.md verbatim.
- [ ] `ACCEPTANCE.md`, `PROGRESS.md`, `DECISIONS.md`, `EXPERIMENTS.md`, `BLOCKERS.md`, `RESULTS.md` exist (BLOCKERS.md may be empty).
- [ ] `docs/GOTCHAS.md` contains all 15 gotchas from §11 of MASTER_PROMPT.md.
- [ ] `docs/FEATURE_DICTIONARY.md` lists every feature from Phase 4.3, Phase 5, and Phase 6.1 with mathematical definition.
- [ ] `pyproject.toml` with strict mypy + ruff config. `mypy --strict src/` passes (vacuously, on empty package).
- [ ] `requirements.txt` has all required dependencies pinned with `==`, including the CUDA PyTorch wheel and (if available) GPU LightGBM.
- [ ] `.gitignore` excludes `output/cache/`, `output/models/*`, `*.pem`, and the other entries from §4.2. Verified: `git status` shows none of these staged.
- [ ] `make precommit` exists and passes.
- [ ] `python -c "import reverse_engineering; print('ok')"` succeeds.
- [ ] **Local compute confirmed**: all dependencies installed locally; nothing installed or run on EC2.
- [ ] **GPU confirmed**: `torch.cuda.is_available()` is `True` and device is `NVIDIA GeForce RTX 3060`. Logged to `RESULTS.md`. (If False → `BLOCKERS.md`, do not advance.)
- [ ] **LightGBM GPU path** checked with a tiny synthetic fit; result (GPU works / CPU-only fallback acceptable) documented in `DECISIONS.md`.
- [ ] **S3 → local sync confirmed**: `make sync` downloads one date/hour partition per feed into `output/cache/`; each lazily readable via `pl.scan_parquet`; sizes logged to `RESULTS.md`.
- [ ] **EC2 reachability confirmed** (health-check only): `ssh -i C:/Users/avych/pm-research-key.pem ubuntu@34.244.229.19 "systemctl is-active pm-clob-collector"` returns a status. No compute run on EC2.
- [ ] AWS credentials load from `c:\users\avych\pm-research\.env`.
- [ ] First commit pushed to `origin main` with message `feat(phase0): bootstrap reverse-engineering project structure (local-compute, GPU verified)`.

**Cannot advance until every box is checked.**

---

## Phase 1 — Data Validation

- [ ] `ohanism_fills.parquet` written to `output/tables/`. Row count ≥ 6,000 per 8 hours of recorded data.
- [ ] Fill count over the recording window reconciles to ohanism's data-api leaderboard / profile within ±0.5%. Discrepancy investigated and root-cause documented in `RESULTS.md`.
- [ ] Realized PnL over the same window reconciles within ±0.1%. Discrepancy investigated.
- [ ] Clock alignment check: 1,000 random OrderFilled events joined to nearest `price_change`. Δt distribution plotted to `output/plots/clock_polygon_vs_pmclob.png`. Median < 5s, p99 < 30s.
- [ ] Clock alignment check: same 1,000 events joined to nearest Binance `aggTrade`. Median Δt < 100ms.
- [ ] orderHash chain stitching produces per-order trajectories. Sample 50; eyeball-confirm in a notebook cell that each trajectory tells a coherent story (order arrives, sits, gets hit / cancelled).
- [ ] Sign discipline: 100 random ohanism fills hand-verified to have `ohanism_side` correctly inverted from raw `OrderFilled.side`. Written test passes.
- [ ] Phase 1 summary written to `RESULTS.md`.
- [ ] Commit pushed with message `feat(phase1): COMPLETE — data validated, reconciliation within tolerance`.

---

## Phase 2 — Maker/Taker Decomposition

- [ ] First-order stats computed and written to `RESULTS.md`:
  - Maker:taker ratio by count and by notional.
  - Side balance per token side (Up vs Down).
  - Buy vs Sell distribution.
  - TTE distribution at fill (histogram).
  - Fills per market distribution.
- [ ] Hypothesis space narrowed. Update §3.1 of `METHODOLOGY.md` (in `WORKING_HYPOTHESIS:` block) with: which of {MM, directional, arb, hybrid} the evidence supports, with quantitative justification.
- [ ] Builder fingerprint computed. If unique, listed in `RESULTS.md`. Any related-wallet candidates surfaced.
- [ ] Plots: maker/taker ratio over time, side balance by hour, builder distribution. All committed to `output/plots/`.
- [ ] Commit: `feat(phase2): COMPLETE — maker/taker decomposed, hypothesis space narrowed to <X>`.

---

## Phase 3 — Order Lifecycle Reconstruction

- [ ] `level_changes.parquet` written. Each row: `(token_id, price, side, t_ns, size_before, size_after, classification ∈ {fill, cancel, partial})`.
- [ ] For each ohanism fill, a reconstructed `pre_fill_trajectory` and `quote_lifetime_ms` available.
- [ ] Pattern classification done for ≥1,000 ohanism quotes: % persistent, % repricing, % pulled.
- [ ] Time-on-book histogram plotted to `output/plots/quote_lifetime_histogram.png`.
- [ ] Quoting-pattern descriptor written to `RESULTS.md`.
- [ ] Commit: `feat(phase3): COMPLETE — order lifecycle reconstructed`.

---

## Phase 4 — Fair Value Modeling (Layers 1 + 2)

### Layer 1 — Regression cascade (diagnostic)
- [ ] `σ_implied` computed for every maker fill with `p ∈ (0.02, 0.98)`. NaN fills excluded.
- [ ] **σ_implied indexed at QUOTE-PLACEMENT-TIME proxy, not fill time** (Phase 3 confirmed
      passive post-once strategy; fill time σ has high variance from market drift).
      Proxy: earliest fill per market ≈ quote placement time.
- [ ] All candidate σ estimators computed at quote-placement-time proxy.
      Stored in `output/tables/features_sigma.parquet`.
- [ ] Best-single-estimator analysis: RMSE table written to `RESULTS.md`.
- [ ] OLS combination fit with HAC standard errors. Adjusted R² reported.
- [ ] Out-of-sample fit reported (train first 12h, test last 12h).
- [ ] Residual diagnostics plotted: vs TTE, vs inventory, vs recent flow. PNGs in `output/plots/`.
- [ ] **REVISED Gate (Phase 3 passive-quoter confirmation): R² ≥ 0.4 at quote-placement-time**
      (down from 0.6). Rationale: a passive post-once quoter has fill times dispersed across
      the market lifetime. σ_implied at fill time has high variance due to market drift
      AFTER quote placement, even if their σ model is perfect. The useful σ is at the moment
      of posting, not at fill time. R² of 0.4 at quote-placement-time is equivalent to
      R² ~0.6 for an active repricing MM. If R² < 0.4 at quote-placement-time proxy,
      the σ model family is wrong — investigate before Layer 2.
- [ ] Market selection check: report fraction of available markets ohanism quoted in
      (currently ~60%; understand selection rule before Layer 2 if <80%).

### Layer 2 — Structural ML estimation
- [ ] Policy `π(state; θ)` written explicitly in `src/reverse_engineering/models/structural_ml.py`. Code comments document the math.
- [ ] Likelihood function written and unit-tested on synthetic data with known θ.
- [ ] Optimization converges from 5 random initializations to the same θ̂ (within tolerance). Hessian positive-definite at optimum.
- [ ] Bootstrap CIs computed (1000 resamples). Reported in `RESULTS.md`.
- [ ] Out-of-sample log-likelihood improves vs Layer 1 OLS.
- [ ] θ̂ written to `output/models/theta_hat.json` with values, CIs, and economic interpretation.
- [ ] Commit: `feat(phase4): COMPLETE — σ recipe identified, structural θ̂ converged`.

---

## Phase 5 — Pricing Adjustments (Layer 3)

- [ ] Inventory skew fit: regression coefficients in `RESULTS.md`. γ recovered.
- [ ] Half-spread function fit. A-S parameters γ, k recovered.
- [ ] Rebate-awareness test run. Result documented.
- [ ] LightGBM trained on Layer 2 residuals. 5-fold time-series CV. Hyperparam grid search done.
- [ ] XGBoost cross-check trained with same setup; OOF score within 5% of LightGBM (sanity check).
- [ ] SHAP analysis complete: global importance, summary, partial dependence for top-3 features, interaction plot for top-2 pairs. All PNGs in `output/plots/`.
- [ ] **Gate: GBT adds ≥5% explained variance over Layer 2**. If not, Layer 2 is already saturated — note this and skip ahead to checking sequential dependencies.
- [ ] Residual autocorrelation tested (Ljung-Box up to lag 30). Result determines whether Phase 6 Layer 4 (sequential) is needed.
- [ ] Commit: `feat(phase5): COMPLETE — pricing adjustments fitted, residuals characterized`.

---

## Phase 6 — Microstructure + Layers 4 & 5

### Microstructure feature analysis
- [ ] Full feature dictionary (§6.1 of METHODOLOGY.md) computed in `src/reverse_engineering/tables/features.py`.
- [ ] Taker direction regression fit. Significant features listed.
- [ ] Maker fill-direction regression fit. Significant features listed.
- [ ] Quote-update regression fit. Internal triggers identified.

### Layer 4 — Sequential (only if Phase 5 found autocorrelated residuals)
- [ ] LSTM fit on rolling windows. Validation NLL recorded.
- [ ] Small Transformer fit. Validation NLL recorded.
- [ ] Likelihood-ratio test vs Layer 3: significant improvement (p < 0.01)?
- [ ] If significant: attention weights / saliency analysis identifies which state variables they're conditioning on. Documented in `RESULTS.md`.
- [ ] If not significant: skip Layer 5 documented and justified.

### Layer 5 — IRL (only if Layers 2-4 still leave systematic residuals)
- [ ] MaxEnt IRL implemented per Ziebart et al.
- [ ] Reward function R(state, action; ψ) recovered.
- [ ] Hypothesis tests run: pure PnL vs Sharpe vs drawdown-penalized vs CARA. p-values reported.
- [ ] If R(ψ̂) differs from Layer 2's implicit reward: Layer 2 must be refit. Note this in `BLOCKERS.md` (it's a workflow back-step, not a true blocker).

- [ ] Commit: `feat(phase6): COMPLETE — microstructure alpha decomposed`.

---

## Phase 7 — Replication + Validation (Layers 6 + 7)

### Layer 6 — Online adaptive
- [ ] Sliding-window structural refit script runs (re-fits Layer 2 hourly on trailing 24h).
- [ ] θ̂ trajectory over recording window plotted (one line per parameter). PNG in `output/plots/theta_trajectory.png`.
- [ ] Drift detected? Reported in `RESULTS.md` with parameter, time of drift, magnitude.

### Layer 7 — Paper twin
- [ ] `OhanismTwin` simulator in `src/reverse_engineering/models/paper_twin.py`. Fully implemented per §7.2 of METHODOLOGY.md.
- [ ] Twin runs end-to-end on the 24h validation window.
- [ ] Match metrics computed: fill count by hour, maker:taker ratio, win rate, PnL by hour, position trajectory Pearson, per-market fill timing KS test.
- [ ] **Acceptance**:
  - PnL within ±10% of real ohanism (target; if missed, document gap and root cause).
  - Fill count within ±20%.
  - Maker:taker ratio within ±5 percentage points.
  - Position trajectory correlation > 0.7.
- [ ] Latency model fit: optimal `ℓ` reported.
- [ ] Capacity caps analyzed.

### Final algorithm document
- [ ] `ALGORITHM.md` written per §6.1 of MASTER_PROMPT.md. Every section populated.
- [ ] Reproducibility manifest at the bottom: git commit, data range, model artifact locations, end-to-end run instructions.
- [ ] Reviewer test: can a stranger read `ALGORITHM.md` and answer "what is ohanism's strategy"? Verified by re-reading the doc as if naive.

- [ ] Final commit: `feat(phase7): COMPLETE — algorithm extracted, paper twin validated, deliverable written`.
- [ ] All commits pushed.

**Phase 7 acceptance = project complete.**

---

## Continuous gates (re-checked at every commit)

These never sunset. Every phase, every commit:

- [ ] All computation ran **locally** (not on EC2). EC2 touched only for S3-sync source or health checks.
- [ ] `mypy --strict src/` passes.
- [ ] `ruff check src/ tests/` passes.
- [ ] `ruff format --check src/ tests/` passes.
- [ ] `pytest -x` passes.
- [ ] `grep -rEn 'TODO|FIXME|XXX|HACK|NotImplementedError|placeholder|"foo"|"bar"' src/` returns zero matches.
- [ ] No `*.pem`, no `output/cache/`, no `.env` staged for commit.
- [ ] `PROGRESS.md` updated.
- [ ] Commit pushed to `origin main`.

If any continuous gate fails, you do not commit; you fix and retry.
