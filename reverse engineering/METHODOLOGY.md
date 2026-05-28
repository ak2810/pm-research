# METHODOLOGY — Working Bible

> Verbatim copy of §9 (METHODOLOGY APPENDIX) from MASTER_PROMPT.md.
> This document governs execution. Do not modify rules; append findings in
> WORKING_HYPOTHESIS blocks below each phase.

---

## PHASE 1 — DATA VALIDATION (DO NOT SKIP)

**Compute & data note** (applies to every phase below). All "Parquet"
references mean the local Parquet cache at `output/cache/`, synced from S3 via
`make sync` / `io/s3_sync.py`. Every computation in every phase runs on the
local workstation (i7-11700K / 64 GB / RTX 3060). EC2 is never a compute node
— only a data source you sync from and a collection host you health-check. Read
big feeds lazily (`pl.scan_parquet` + pushdown), process per-hour or
per-market, stream large joins (§2.14). GPU (`device="gpu"` for LightGBM in
Layer 3; CUDA PyTorch for Layer 4) is local.

**Data-shape note** (verified against the live collectors). The pipeline
rotator writes pm_clob / polygon / binance / pm_meta with inferred Parquet
schema (no casting). Consequences you must handle: (a) all money/price/size
fields (`*_decimal`, `price`, `size`, `*_amount_decimal`, `fee_decimal`,
`best_bid`, `best_ask`) are stored as strings — parse to `decimal.Decimal` on
read, never via float; (b) `*_raw` fields are uint256 decimal strings; (c)
nested fields (`price_changes`, `bids`, `asks`, `pm_meta` event/market, binance
depth/kline arrays) are JSON strings — `json.loads` them; pm_meta
`market.clobTokenIds` is double-encoded; (d) timestamps `t_recv_ns` are int64
ns; (e) polygon rows carry no block timestamp — derive `t_block_ns` from
`block_number` via RPC (gotcha #16); (f) token identity key = uint256 decimal
string, identical across polygon `token_id`, pm_clob `asset_id`, pm_meta
`clobTokenIds`.

Before any modeling. One missing fill or one mis-timestamped feed and every
downstream coefficient is biased.

### 1.1 Reconcile fill counts and PnL

From your Polygon Parquet (local cache): every `OrderFilled` where `maker ==
0x89b5...` or `taker == 0x89b5...` (lowercase compare) on both `0xE111…` (CTF
V2) and `0xe222…` (Neg Risk V2 — should be empty for these markets, verify).
Note maker/taker in the data are already lowercase 0x addresses; exchange
distinguishes the two contracts.

Add `TransferSingle` from Conditional Tokens (`0x4D97…`) where `from_` or `to`
matches the proxy, including the signer EOA (resolve via `ProxyCreated` event
on factory `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052`; the parent project's
`wallet_attribution.py` already uses topic
`0x4f51faf6…e235` for this — verify that topic before relying on it).

Resolve the proxy/signer via the verified endpoint `GET
https://data-api.polymarket.com/v1/leaderboard?userName=ohanism` (returns
`proxyWallet`). The `data-api.polymarket.com/profile?username=` path returns
404 — do not use it (per VERIFIED_FACTS.md). For the per-window fill count and
PnL ground truth, VERIFIED_FACTS does not pin a fills/positions endpoint —
verify the correct data-api activity/positions endpoint live (capture a sample
response, save to `notes/VERIFIED_FACTS_RE.md`) before trusting it.

Pick a fixed 24h window (you now have >24h recorded) so the reconciliation
target does not move while you debug.

**Acceptance**: fill count within ±0.5%, PnL within ±0.1%. Do not start
modeling until these pass.

### 1.2 Clock alignment across feeds

First, build the `block_number → t_block_ns` map by querying
`eth_getBlockByNumber` for every distinct block in your window (cache it). This
is your authoritative on-chain clock — the recorded `t_recv_ns` on polygon rows
is NOT the block time and is wall-clock garbage for any backfilled rows.

For 1,000 random `OrderFilled` events: find the nearest
`price_change`/`last_trade_price` in pm_clob with matching (`asset_id`, `price`,
`side`). Compare against derived `t_block_ns`. Δt distribution should be tight,
centered near Polygon block confirmation time (~2s). Rows where polygon
`t_recv_ns` disagrees with derived `t_block_ns` by >10s are backfilled —
exclude their `t_recv_ns` from any timing use.

For the same 1,000: find the nearest Binance `bookTicker`/`aggTrade` on the
matching symbol (Binance feed time = `t_recv_ns`, which is live and reliable;
`aggTrade` also has `E`/`T` ms event times). Δt should be small.

**Critical**: timestamp the decision, not the settlement. The bot decided before
the block. Use `t_ws_ns` — the `price_change`/`last_trade_price` receive time
that announced the fill on the WS — as the decision clock. The derived block
time is the settlement clock; the WS time is closer to the bot's actual
decision.

### 1.3 orderHash chain stitching

For each ohanism fill, the same `orderHash` may appear in multiple
`price_change` events before the fill (size decrementing as partially hit).
Stitch into a per-order lifecycle. Required for Phase 3. Note: `order_hash` is
recorded on every `OrderFilled` (it's `topics[1]`, a bytes32 hex with `0x`),
and `hash` appears on pm_clob `price_change` entries (a different hash — the
level hash, no `0x`); do not conflate them.

### 1.4 Sign discipline on OrderFilled.side (VERIFY EMPIRICALLY — do not assume)

`side` is recorded as a raw uint8 (0 or 1) per the V2 ABI enum `Side
{BUY=0, SELL=1}`. Whose side it refers to (the maker order being filled, vs the
taker) is NOT documented in VERIFIED_FACTS and you must determine it
empirically — do not hardcode an assumption. Method:

- Take a sample of ohanism fills where you also see the corresponding pm_clob
  activity.
- Reconstruct ohanism's resulting position two ways (assuming side =
  maker-order side, vs side = taker side) and carry both candidate position
  series.
- Reconcile each candidate against ohanism's public profile PnL and against
  `PositionsMerge`/`PayoutRedemption` events. The interpretation that
  reconciles within tolerance is the correct one. Record the verified
  interpretation in `notes/VERIFIED_FACTS_RE.md` with the evidence.
- Only then collapse to a single `ohanism_side` column. This is the #1 source
  of sign-flip bugs — getting it wrong silently corrupts every inventory and
  PnL number downstream.

---

## PHASE 2 — MAKER/TAKER DECOMPOSITION

**Goal**: a single table where every row is one ohanism trade, tagged with
everything.

### 2.1 Build ohanism_fills table

Columns (parquet):

- `block_number`, `log_index` (primary key — these ARE recorded by the polygon
  indexer)
- `t_recv_ns` (the indexer's receive time — see codebase note below; not the
  block timestamp)
- `t_block_ns` (the true block timestamp — NOT in the recorded data; you MUST
  derive it, see below)
- `t_ws_ns` (the pm_clob `price_change`/`last_trade_price` receive time that
  announced this fill — your most reliable decision-time clock)
- `token_id`, `market` (token_id = the uint256 decimal string; identical key
  across polygon `token_id`, pm_clob `asset_id`, pm_meta `clobTokenIds`)
- `asset_symbol`, `horizon` (BTC/ETH/SOL/XRP/DOGE; 5m/15m/1h — resolve from
  pm_meta slug)
- `is_maker` (bool)
- `ohanism_side` (BUY=long token, SELL=short token, ohanism's perspective —
  derive carefully; verify empirically, see gotcha #1)
- `outcome_side` ("Up" or "Down")
- `price` (Decimal; derived from amount ratio)
- `size` (Decimal, token units)
- `fee_paid` (Decimal; 0 if maker)
- `rebate_received` (Decimal; 0.2 × implied fee if maker)
- `time_to_expiry_s` (`endDate − t_block`; `endDate` from pm_meta)
- `start_strike_price` (the spot at market open — NOT in pm_meta; you MUST
  derive it from the spot feed at the market's `startDate`, see below)
- `builder`, `metadata` (V2 bytes32, stored as hex WITHOUT `0x` prefix in the
  data; for §10)

**CODEBASE COHERENCE NOTES** (verified against the live collectors — these
override any assumption to the contrary):

- There is no `block_timestamp` in the polygon feed. The indexer's record
  carries only `block_number`, `block_hash`, `tx_hash`, `log_index`,
  `t_recv_ns`. For live logs (`eth_subscribe`) `t_recv_ns` ≈ block time +
  propagation and is usable. For backfilled logs `t_recv_ns` is the wall-clock
  at backfill time and is garbage for alignment. Therefore: derive the
  authoritative `t_block_ns` by querying the Polygon RPC
  `eth_getBlockByNumber` for each distinct `block_number` (bounded set; cache
  the `block_number→timestamp` map to `output/cache/block_times.parquet`). Use
  that derived `t_block_ns` everywhere a block time is needed. Do NOT trust
  `t_recv_ns` on polygon rows for timing without first checking it against the
  derived block time (flag rows where they disagree by >10s as backfilled).
- `start_strike_price` is not in the metadata. The pm_meta market object has
  `bestBid`/`bestAsk`/`lastTradePrice` but no spot-at-open. The strike for an
  Up/Down market is the reference price at the market's `startDate`. Derive it
  from the spot feed at `startDate`. Resolution-source caveat (verified): 5m
  and 15m markets resolve on the Chainlink BTC/USD (etc.) stream; hourly
  markets resolve on the Binance 1-hour candle. You only record Binance. So for
  5m/15m, Binance spot is a proxy for the true Chainlink strike — quantify the
  Chainlink↔Binance basis as a known residual source (gotcha #17) and, where
  possible, validate by reconstructing a sample of resolved markets' outcomes
  from Binance and checking against the on-chain `ConditionResolution`.
- Inferred-schema parquet: nested fields are JSON strings. The rotator writes
  pm_clob, polygon, binance, pm_meta with inferred schema (no cast). Any nested
  value was JSON-stringified at write time. So in the parquet: pm_clob
  `price_changes`, `bids`, `asks` are JSON strings (`json.loads` them); pm_meta
  `event` and `market` are JSON strings, and inside the decoded `market`,
  `clobTokenIds` is itself a JSON-string-of-array (double-encoded — parse
  twice). Binance depth/kline nested `b`/`a`/`k` are JSON strings; `bookTicker`
  and `aggTrade` scalar fields are native.
- Feeds present in S3: `pm_clob`, `polygon`, `binance`, `pm_meta`, and also
  `wallet` (the daily wallet-graph dump). You read the first four; `wallet` is
  optional context.

This table is the spine of everything downstream.

### 2.2 First-order statistics

Compute and write to RESULTS.md:

- Maker:taker ratio by count and by notional. >70% maker → MM hypothesis holds.
- Side balance by token side (Up vs Down). Symmetric → delta-neutral by
  construction; skewed → directional.
- Buy vs Sell on each token. Both → real MM; one → directional.
- Distribution of `time_to_expiry_s` at fill. Long TTE-heavy → patient quoter;
  near-expiry → late-leg directional.
- Fills per market. 1-2 → closing; 10+ → continuous quoting.

### 2.3 Builder/metadata fingerprint

- Top 5 most common `builder` values for ohanism fills.
- Top 5 across all fills on the same markets.
- If ohanism has a unique/near-unique builder, that's a fingerprint to find
  their other addresses (any wallet with the same builder is likely related).

---

## WORKING_HYPOTHESIS after Phase 2

*(Append findings here after Phase 2 completes.)*

---

## PHASE 3 — ORDER LIFECYCLE RECONSTRUCTION

### 3.1 Quote inference from price_change stream

`price_change.size` = new resting size at that level (not delta). Build
`level_changes`:

- For each `(token_id, price, side, t_recv_ns)`, the size delta from the
  previous observation.
- Cross-reference with the next ~5 blocks' `OrderFilled` events on that token
  at that price. If `maker == ohanism` and `maker_amount / 10^6 == delta`,
  attribute as fill. Otherwise classify as cancel (V2 has no on-chain
  cancellation).

### 3.2 Per-order trajectory

For each ohanism fill, look backward:
- Did the level size increase shortly before? → their order arrived.
- How long was it resting? → quote lifetime (level elevated above pre-arrival
  baseline).
- Did Binance spot move during that rest? Did they reprice (size drops at price
  A, size rises at adjacent price B in same WS frame)?

Classify each quote into one of three patterns and tabulate the proportion:
- **Persistent** — sits 1-5s, gets hit. Passive MM tolerating adverse selection.
- **Repricing** — moves to new level when spot moves by Δ. Continuous
  fair-value pulled from spot.
- **Pulled** — disappears when spot moves against them, reappears later.
  Defensive MM with vol-of-vol gating.

### 3.3 Time-on-book distribution

Histogram of inferred quote lifetimes:
- <100ms sharp mode → post-and-immediately-cancel (probing).
- 1-5s mode → real quoting with vol-aware lifetime.
- 30s+ tail → patient + small.

---

## PHASE 4 — FAIR VALUE MODELING (LAYERS 1 + 2)

Up/Down markets are digital options with strike = spot at market open. Fair
value is `P(S_T > S_0 | S_t)` — closed-form in (current spot, start spot,
time-to-expiry, σ). The bot's edge against the public is almost entirely a
better σ estimator.

### 4.1 Base formula (GBM, zero drift over the horizon)

```
P(Up)  =  1 − Φ(d)
d      =  log(S_0 / S_t) / (σ × sqrt(T − t))
```

### 4.2 Implied σ extraction

For every ohanism maker fill, invert the formula:

- **Known**: fill price `p`, start spot `S_0`, spot at fill time `S_t` (from
  Binance `bookTicker` mid), remaining `τ = T − t`.
- **Solve**: σ such that `1 − Φ(log(S_0/S_t) / (σ√τ)) = p`. Use a numerical
  root finder (Brent's method) bounded to `σ ∈ (1e-6, 10)`.
- Call this `σ_implied`.

If they're a pure fair-value quoter, `σ_implied ≈` their σ estimate at that
moment, possibly shifted by inventory and rebate.

### 4.3 Candidate σ estimators (compute for each fill)

- `σ_rv_W` for `W ∈ {1s, 5s, 30s, 60s, 300s, 900s}`: realized vol of 100ms
  Binance `bookTicker` mid log-returns.
- `σ_ewma_λ` for `λ ∈ {0.94, 0.97, 0.99}`: RiskMetrics EWMA on log-returns.
- `σ_garch`: GARCH(1,1) per symbol per day (use `arch` library), evaluated at
  t.
- `σ_seasonal`: hour-of-day rescaling × baseline σ (crypto vol is highly
  seasonal — Asia/EU/US opens).
- `σ_intraday_intensity`: "event time" σ proxy using trade arrival count instead
  of clock time.
- `σ_klines`: Parkinson estimator from Binance `kline_1m` candle ranges.

### 4.4 Identify the σ recipe — LAYER 1 (regression cascade, diagnostic)

**(a) Best single estimator**

For each σ candidate `E`, compute `RMSE(σ_implied − σ_E)`. Lowest wins. Likely
a combination, but tells you the dominant ingredient. Expect `σ_ewma_0.97` over
30-60s to be competitive.

**(b) Linear combination via OLS**

```
σ_implied ≈ α + β_1 σ_rv_5s + β_2 σ_rv_30s + β_3 σ_ewma_0.97 + β_4 σ_seasonal + ε
```

HAC / Newey-West standard errors (fills are autocorrelated within a market).
Report adjusted R² and out-of-sample fit (train first 12h, test last 12h).

**Acceptance for Phase 4 Layer 1**: R² > 0.6, residuals look approximately
unstructured. If not, the model family is wrong — investigate before proceeding
to Layer 2.

### 4.5 Residual bias checks

After fitting, plot `σ_implied − σ_fitted` vs:
- Time-to-expiry (do they widen σ near expiry for pin risk?).
- Inventory (long inventory should lower their σ → directional response, not
  part of σ).
- Recent flow direction (do they bake in short-term order-flow drift?).

Autocorrelated residuals → state they're using that you haven't included
(realized skew, jump indicators, funding-rate proxies).

### 4.6 LAYER 2 — Maximum-likelihood structural estimation (the spine)

Write the bot's quoting policy as `π(state; θ)` with parameters:

- σ-recipe weights `w = (w_1, ..., w_k)` over candidate σ estimators, with
  `Σ w_i = 1`, `w_i ≥ 0`.
- Inventory aversion `γ > 0`.
- Half-spread function: `half_spread(σ, τ, intensity; a, b, c) = a × σ × √τ +
  b × intensity + c`.
- Rebate sensitivity `ρ ∈ [0, 1]`.
- Latency `ℓ ≥ 0`.
- Inventory cap `q_max > 0`.

**Likelihood**:

```
L(θ) = Π_fills P(observed fill | market state at decision time, θ)
```

Specify the fill model: given a posted quote at price `q` against a
counterparty arrival process with intensity `λ(q − fair)`, the probability of
fill in window `dt` is `λ(...) dt`. For taker fills: probability of crossing is
a function of the spot-PM basis and microstructure signals.

Maximize log-likelihood. Constraints enforced via reparameterization (softmax
for `w`, log for `γ`, etc.). Use `scipy.optimize.minimize` with BFGS-B or
trust-constr. Bootstrap for confidence intervals (1000 resamples).

**Acceptance for Phase 4 Layer 2**: BFGS converged with positive-definite
Hessian; θ̂ has finite standard errors; bootstrap CIs are tight; out-of-sample
log-likelihood improves vs Layer 1 OLS.

---

## PHASE 5 — PRICING ADJUSTMENTS (LAYER 3)

### 5.1 Inventory skew (within Layer 2; also tested separately)

Build running ohanism position by `token_id` across all available history (use
Conditional Tokens `TransferSingle`/`Batch` + `OrderFilled` deltas). At each
fill, compute net position in that market. Regress:

```
(their_quote_mid − fair_value) ~ position_in_token + position_total_dollar_exposure + ε
```

Negative coefficient on `position_in_token` = inventory aversion. Magnitude = γ
in A-S terms.

### 5.2 Half-spread function

Compute the gap between their bid and ask quotes when both present. Fit:

```
half_spread ~ σ × √τ + λ × order_arrival_intensity + ε
```

Avellaneda-Stoikov form: `half_spread = (γ σ² τ)/2 + (1/γ) log(1 + γ/k)`.
Recover γ and k both.

### 5.3 Rebate awareness

Breakeven maker price is shifted by `0.2 × 0.07 × min(p, 1−p) ≈ 0.014 ×
min(p, 1−p)`. Test whether quotes systematically sit ~1.4 ticks inside fair at
p=0.5 vs ~0.3 ticks at p=0.1.

### 5.4 LAYER 3 — Gradient-boosted residual model

Train LightGBM (and XGBoost as cross-check) on:
- **Target**: residual from Layer 2 = observed quote − `π(state; θ̂)`.
- **Features**: full dictionary from Phase 6 below.
- 5-fold time-series cross-validation (preserve order).
- Hyperparameter search: small grid (`max_depth ∈ {3,5,7}`, `learning_rate ∈
  {0.01,0.05,0.1}`, `n_estimators ∈ {500,2000}`).

Compute SHAP values; produce:
- Global feature importance bar chart.
- SHAP summary plot.
- Top-3 feature partial dependence plots.
- SHAP interaction plot for top-2 interactions.

Findings to log: which features drive residuals, in which direction, with which
interactions. These are the nonlinearities Layer 2 missed.

**Acceptance for Phase 5**: GBT explains an additional ≥5% of variance over
Layer 2; residuals from Layer 3 are uncorrelated or only
sequentially-correlated (advance to Layer 4 in Phase 6).

---

## PHASE 6 — MICROSTRUCTURE ALPHA + LAYERS 4 & 5

### 6.1 Feature dictionary (computed strictly before t_recv_ns)

- **Spot–PM basis**: `binance_mid − PM_implied_spot_via_fair_value`. Reconstruct
  what spot the PM mid implies, compare to actual.
- **Cross-venue lead-lag**: signed Binance return over {100ms, 500ms, 1s, 5s}.
- **PM book imbalance**: `log((Σ bid_sizes within 2 ticks of mid) / (Σ ask_sizes
  within 2 ticks))`.
- **Recent PM taker flow**: signed taker volume on this token over {1s, 5s, 30s}.
- **Realized vol regime**: percentile rank of current σ vs trailing 24h on this
  symbol.
- **Time-of-day**: hour-bucket dummies.
- **Cross-asset moves**: BTC return as feature for ETH market (correlated alts
  lag by 50-500ms).
- **Resolution boundary distance**: at TTE<60s, `|spot − start| / start`. Pin
  risk explodes at small values.

### 6.2 Direction regression (taker fills)

```
sign(ohanism_size) ~ features  (probit / logistic)
```

Marginal effects = expected edge per unit of signal.

### 6.3 Maker fill-direction regression

```
fill_indicator_within_next_5s ~ features × side  (conditional on quote being live)
```

### 6.4 Quote-update regression

Regress quote-update events on:
- Spot move magnitude over last N ms.
- σ change over last N seconds.
- Book imbalance shift.
- Their inventory change.

Predictive features = internal triggers for their refresh logic.

### 6.5 LAYER 4 — Sequential / state-dependent model (only if Layer 3 residuals are autocorrelated)

Fit an LSTM and a small Transformer (4-6 layers, attention) on rolling windows
of `(state_t-k, ..., state_t) → action_t`. Predict action conditional on recent
history.

If recurrent significantly outperforms Layer 3 on held-out data (LR test,
validation NLL), they have stateful behavior. Use attention weights / saliency
to identify what they're conditioning on. Likely candidates: session P&L
target, drawdown brake, strategy mode switching.

### 6.6 LAYER 5 — Inverse Reinforcement Learning (only if Layers 2-4 still leave residuals)

MaxEnt IRL (Ziebart et al.) or Bayesian IRL. Treat ohanism's observed (state,
action) sequence as expert demonstrations. Recover the reward function
`R(state, action; ψ)`.

Test hypotheses:
- Is the reward pure PnL, or Sharpe-adjusted PnL?
- Is there a drawdown penalty?
- Risk-neutral or CARA-risk-averse?

If `R(ψ̂)` differs significantly from the implicit "maximize expected PnL with
γ-inventory-penalty" of Layer 2, you've recovered an objective shift. Layer 2
must be refit with the new objective.

---

## PHASE 7 — REPLICATION + VALIDATION (LAYERS 6 + 7)

### 7.1 LAYER 6 — Online adaptive replication

Re-fit Layer 2's structural estimation on a sliding 24h window, every hour.
Track θ̂ over time. Plot trajectories. Drift in γ → they got more risk-averse
(saw losses?). Drift in σ-recipe weights → they recalibrated vol model. Sudden
jumps → they shipped a code change.

The trajectory of θ̂ is itself an artifact; write it to
`output/results/theta_trajectory.parquet`.

### 7.2 LAYER 7 — Paper twin

`OhanismTwin` simulator in
`src/reverse_engineering/models/paper_twin.py`. At each tick, given the public
state:

1. Compute σ from the fitted recipe (Layer 2 weights, possibly Layer 4
   adjustment).
2. Compute fair value via the digital formula.
3. Apply inventory skew (Layer 2 γ).
4. Apply half-spread (Layer 2 a, b, c).
5. Apply microstructure adjustments (Layer 3 residual + Layer 4 sequential,
   if applicable).
6. Output quotes on Up and Down sides.
7. Simulate fills against the actual book state at that moment (counterparty
   arrival = actual orders that crossed your simulated price).
8. Update inventory.

### 7.3 Match metrics (twin vs real ohanism, 24h window)

- Fill count by hour.
- Maker:taker ratio.
- Win rate (fill vs final mark).
- Realized PnL by hour.
- Position trajectory correlation.
- Per-market fill timing distribution.

**Acceptance target**: PnL within ±10% over 24h; fill count within ±20%;
maker:taker ratio within ±5 percentage points; position trajectory Pearson
correlation > 0.7.

### 7.4 Latency model

You will have worse RTT than they do. Add a latency parameter ℓ to the twin;
sweep over plausible values; report which ℓ best matches their fill timing
distribution. That value is their inferred latency advantage.

### 7.5 Capacity caps

Look at the distribution of their per-fill sizes vs available depth at that
moment. They may run smaller sizes than what the market clears (defensive).
Document any size-capping behavior.

---

## §10 — IDENTIFYING THEIR STACK

### 10.1 Builder field

V2 `OrderFilled.builder` is non-zero for orders routed through specific
operators. Three classes:
- `0x00...` (direct submission) — self-relay, likely runs own infra against the
  CLOB REST API.
- Known aggregator builder — they route through a third party.
- Custom builder (own proxy contract) — uncommon, sophisticated.

### 10.2 Quote-update timing

Inter-arrival distribution of their `price_change` events on a single token
while quoting:
- Sharp 100ms grid → fixed-clock polling.
- Reactive to Binance updates (cross-correlation with Binance event stream) →
  event-driven (better setup).

### 10.3 The signer EOA

Resolve via `ProxyCreated(address proxy, address signer)` on
`0xaB45c5A4B0c941a2F231C04C3f49182e1A254052`. Investigate the signer EOA's
other on-chain activity (Permit2 use, signing-service patterns, gas-payment
patterns).

### 10.4 Submission rate vs cancellation rate

Cancellations aren't on-chain in V2, but you inferred them in Phase 3.1. Ratio
(submissions / cancellations) is a stack identifier — 1:50 = speculative
posting; 1:5 = thoughtful and patient.

---

## §11 — GOTCHAS (see also docs/GOTCHAS.md)

See `docs/GOTCHAS.md` for the full numbered list.

---

## §12 — ONE-PAGE CHEAT SHEET

| Question | How to answer | Why |
|---|---|---|
| MM or directional? | Maker:taker ratio + side balance (Phase 2) | Settles hypothesis space |
| What σ are they using? | OLS of σ_implied on candidate estimators (Phase 4.4) | Core of the alpha |
| How risk-averse? | Coefficient on position_in_token (Phase 5.1) → γ | A-S parameter |
| Spot lead-lag? | Significance of Binance-return features (Phase 6) | Reactive vs predictive |
| Rebate-aware? | Quote offset from fair vs min(p, 1-p) (Phase 5.3) | Edge-source decomposition |
| Sequential state? | LSTM/Transformer beats GBT on residuals (Phase 6.5) | Stateful policy |
| Different objective? | IRL recovers reward ≠ raw PnL (Phase 6.6) | Sharpe / drawdown penalty / CARA |
| How to replicate? | Twin match metrics (Phase 7.3) | The deliverable |
| Strategy drift? | θ̂ trajectory over time (Phase 7.1) | When do they tune |
