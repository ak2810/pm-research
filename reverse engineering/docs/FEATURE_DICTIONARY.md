# FEATURE DICTIONARY

Every feature used in Layers 1-5, with mathematical definition and formula.
All features computed strictly before `t_recv_ns` of the fill (no lookahead).

---

## σ Estimators (Phase 4.3)

Let `m_t = log(mid_t)` be the log mid-price from Binance bookTicker.

### σ_rv_W — Realized Volatility, window W seconds

```
σ_rv_W(t) = sqrt( (1/n) * Σ_{i=1}^{n} r_i^2 ) * sqrt(annualization_factor)
```

where `r_i = m_{t_i} - m_{t_{i-1}}` are 100ms log-returns sampled backwards
from `t` over window `W`. Annualization: `sqrt(252 * 24 * 3600 / dt_s)` where
`dt_s = 0.1s`.

Windows: W ∈ {1, 5, 30, 60, 300, 900} seconds.

### σ_ewma_λ — RiskMetrics EWMA Volatility

```
h_t = λ * h_{t-1} + (1-λ) * r_t^2
σ_ewma_λ(t) = sqrt(h_t) * sqrt(annualization_factor)
```

λ ∈ {0.94, 0.97, 0.99}. Updated on each 100ms Binance tick.

### σ_garch — GARCH(1,1)

```
r_t = ε_t * sqrt(h_t)
h_t = ω + α * r_{t-1}^2 + β * h_{t-1}
```

Fit daily (one fit per symbol per calendar day) on 1-second log-returns using
the `arch` library. Evaluated at fill time `t` by recursing forward from the
last fitted state.

### σ_seasonal — Hour-of-Day Seasonally Adjusted

```
σ_seasonal(t) = σ_base * f(hour(t))
```

where `σ_base` is the 24h realized vol and `f(h)` is the median σ_rv_60s in
hour bucket `h` normalized to have mean 1 across all hours. Captures the
Asia/EU/US session vol pattern.

### σ_intraday_intensity — Event-Time Vol Proxy

```
σ_intraday_intensity(t) = σ_rv_60s(t) * sqrt(N_t / N_baseline)
```

where `N_t` = aggTrade count in last 60s, `N_baseline` = median aggTrade count
per 60s over the trailing day. Adjusts for trade-arrival clustering.

### σ_klines — Parkinson Estimator

```
σ_klines(t) = sqrt( (1/(4 * n * ln(2))) * Σ_{i=1}^{n} [ln(H_i/L_i)]^2 ) * sqrt(annualization)
```

where `H_i`, `L_i` are the high and low of the i-th `kline_1m` candle. `n` =
number of complete 1m candles in the past W minutes (default W=15).

---

## Microstructure Features (Phase 6.1)

### spot_pm_basis — Spot–PM Basis

```
spot_pm_basis = binance_mid(t) - PM_implied_spot(t)
```

`PM_implied_spot(t)` is the spot price implied by the current PM mid-price
under the GBM fair-value formula:

```
p_mid = 1 - Φ(log(S_0 / S_implied) / (σ_ewma_097 * sqrt(τ)))
=> S_implied = S_0 * exp(σ_ewma_097 * sqrt(τ) * Φ^{-1}(1 - p_mid))
```

### binance_ret_Nms — Cross-Venue Lead-Lag Returns

```
binance_ret_Nms(t) = log(mid(t)) - log(mid(t - N ms))
```

N ∈ {100, 500, 1000, 5000} ms. Signed: positive = Binance rose, PM may lag.

### pm_book_imbalance — PM Book Imbalance

```
pm_book_imbalance = log(bid_depth_2ticks / ask_depth_2ticks)
```

where `bid_depth_2ticks = Σ size_i for all bid levels within 2 ticks of best bid`
and similarly for asks. Positive = more bid depth.

### pm_taker_flow_Ns — Recent PM Taker Flow

```
pm_taker_flow_Ns = Σ_{fills in [t-N, t]} sign(taker_side) * size
```

where `sign(taker_side)` = +1 if taker bought (BUY), -1 if taker sold (SELL).

### sigma_regime_pctile — Realized Vol Percentile

```
sigma_regime_pctile = rank(σ_ewma_097(t)) / count(σ_ewma_097 in trailing 24h)
```

Ranges [0, 1]. High = elevated vol environment.

### hour_bucket — Hour of Day

```
hour_bucket = floor(t_ns / 3_600_000_000_000) % 24
```

Integer 0-23 UTC. Used as dummy variable or embedding index.

### btc_ret_1s — BTC Cross-Asset Return

```
btc_ret_1s(t) = log(BTC_mid(t)) - log(BTC_mid(t - 1s))
```

Feature for non-BTC markets (ETH, SOL, XRP, DOGE). BTC often leads altcoin
moves by 50-500ms.

### resolution_distance — Pin Risk Proximity

```
resolution_distance(t) = |spot(t) - start_strike| / start_strike
  if time_to_expiry_s(t) < 60 else NaN
```

Small value near expiry = near-ATM = high gamma / pin risk.

### tte_s — Time to Expiry

```
tte_s = (endDate_ns - t_block_ns) / 1_000_000_000
```

In seconds. Core input to the GBM fair-value formula.

### ohanism_inventory — Net Token Inventory

```
ohanism_inventory(t) = Σ_{fills before t} signed_size(fill)
```

Running sum of ohanism's signed position in the specific token_id.
`signed_size = +size if ohanism_side=BUY, -size if ohanism_side=SELL`.

### ohanism_total_exposure — Dollar Exposure

```
ohanism_total_exposure(t) = Σ_{token_id} |ohanism_inventory(token_id, t)| * price(token_id, t)
```

Sum of absolute inventory × current mark across all active tokens.

---

## Derived Policy Targets (for Layer 2)

### fair_value_up — GBM Digital Call Fair Value

```
fair_value_up = 1 - Φ(d)
d = log(S_0 / S_t) / (σ * sqrt(τ))
```

where `S_0 = start_strike_price`, `S_t = binance_mid(t)`, `τ = tte_s / (365 * 24 * 3600)`.

### half_spread_as — Avellaneda-Stoikov Half-Spread

```
half_spread_as = (γ * σ^2 * τ) / 2 + (1/γ) * log(1 + γ/k)
```

Parameters γ (risk aversion), k (market depth) recovered in Phase 5.2.

### inventory_skew — A-S Inventory Adjustment

```
inventory_skew = γ * σ^2 * τ * ohanism_inventory
```

Applied as a downward shift to the mid-quote when ohanism is long.

### rebate_breakeven_shift — Rebate Awareness Correction

```
rebate_breakeven_shift = rebate_rate * fee_rate * min(p, 1-p)
                       = 0.2 * 0.07 * min(p, 1-p)
                       = 0.014 * min(p, 1-p)
```

If ohanism posts at `fair + rebate_breakeven_shift`, they earn zero net fee
(rebate offsets fee exactly). Quotes systematically inside this point imply
rebate-maximizing behavior.
