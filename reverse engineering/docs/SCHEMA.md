# SCHEMA — Output Tables

All tables written to `output/tables/` as Parquet. Primary keys noted.
Money/price/size columns stored as UTF8 strings representing Decimal values
with 6 decimal places. Timestamps as int64 nanoseconds.

---

## ohanism_fills

Primary key: `(block_number, log_index)`

| Column | Type | Description |
|--------|------|-------------|
| `block_number` | UInt64 | Polygon block number |
| `log_index` | UInt32 | Log index within block |
| `t_recv_ns` | Int64 | Indexer receive time (ns); backfill-check vs t_block_ns |
| `t_block_ns` | Int64 | Derived block timestamp (ns); authoritative on-chain clock |
| `t_ws_ns` | Int64 | pm_clob WS receive time of matching price_change (ns) |
| `token_id` | Utf8 | uint256 decimal string; consistent across all feeds |
| `market` | Utf8 | 0x-prefixed condition_id / market hex |
| `asset_symbol` | Utf8 | BTC / ETH / SOL / XRP / DOGE |
| `horizon` | Utf8 | 5m / 15m / 1h |
| `is_maker` | Boolean | True if ohanism placed the resting order |
| `ohanism_side` | Utf8 | BUY (long token) or SELL (short token), ohanism's perspective |
| `outcome_side` | Utf8 | Up or Down |
| `price` | Utf8 | Fill price as Decimal string (6 dp) |
| `size` | Utf8 | Fill size in token units as Decimal string (6 dp) |
| `fee_paid` | Utf8 | Fee paid as Decimal string (6 dp); 0.000000 if maker |
| `rebate_received` | Utf8 | Maker rebate as Decimal string (6 dp); 0.000000 if taker |
| `time_to_expiry_s` | Float64 | Seconds from t_block_ns to market endDate |
| `start_strike_price` | Utf8 | Spot price at market startDate, from Binance bookTicker mid |
| `builder` | Utf8 | bytes32 hex without 0x prefix (32 zero bytes = direct) |
| `metadata` | Utf8 | bytes32 hex without 0x prefix |

---

## level_changes

Primary key: `(token_id, price, side, t_recv_ns)`

| Column | Type | Description |
|--------|------|-------------|
| `token_id` | Utf8 | uint256 decimal string |
| `price` | Utf8 | Price level as Decimal string |
| `side` | Utf8 | BUY or SELL |
| `t_recv_ns` | Int64 | WS receive time of the price_change event (ns) |
| `size_before` | Utf8 | Resting size before this event (Decimal string) |
| `size_after` | Utf8 | New resting size (Decimal string); "0.000000" = level removed |
| `delta` | Utf8 | size_after − size_before as Decimal string (signed) |
| `classification` | Utf8 | fill / cancel / partial / new_order |
| `fill_block_number` | UInt64 | If classification=fill: matching OrderFilled block_number |
| `fill_log_index` | UInt32 | If classification=fill: matching OrderFilled log_index |

---

## features_sigma

| Column | Type | Description |
|--------|------|-------------|
| `block_number` | UInt64 | Join key to ohanism_fills |
| `log_index` | UInt32 | Join key to ohanism_fills |
| `sigma_implied` | Float64 | Implied σ from inversion of digital formula |
| `sigma_rv_1s` | Float64 | Realized vol, 1s window |
| `sigma_rv_5s` | Float64 | Realized vol, 5s window |
| `sigma_rv_30s` | Float64 | Realized vol, 30s window |
| `sigma_rv_60s` | Float64 | Realized vol, 60s window |
| `sigma_rv_300s` | Float64 | Realized vol, 300s window |
| `sigma_rv_900s` | Float64 | Realized vol, 900s window |
| `sigma_ewma_094` | Float64 | EWMA vol, λ=0.94 |
| `sigma_ewma_097` | Float64 | EWMA vol, λ=0.97 |
| `sigma_ewma_099` | Float64 | EWMA vol, λ=0.99 |
| `sigma_garch` | Float64 | GARCH(1,1) fitted vol |
| `sigma_seasonal` | Float64 | Hour-of-day seasonally adjusted vol |
| `sigma_intraday_intensity` | Float64 | Event-time vol proxy |
| `sigma_klines` | Float64 | Parkinson estimator from kline_1m |

---

## features_full

(All sigma columns above, plus microstructure features from Phase 6.1.)

| Column | Type | Description |
|--------|------|-------------|
| `spot_pm_basis` | Float64 | binance_mid − PM_implied_spot |
| `binance_ret_100ms` | Float64 | Signed Binance log-return, 100ms |
| `binance_ret_500ms` | Float64 | Signed Binance log-return, 500ms |
| `binance_ret_1s` | Float64 | Signed Binance log-return, 1s |
| `binance_ret_5s` | Float64 | Signed Binance log-return, 5s |
| `pm_book_imbalance` | Float64 | log(bid_depth_2ticks / ask_depth_2ticks) |
| `pm_taker_flow_1s` | Float64 | Signed taker volume, 1s |
| `pm_taker_flow_5s` | Float64 | Signed taker volume, 5s |
| `pm_taker_flow_30s` | Float64 | Signed taker volume, 30s |
| `sigma_regime_pctile` | Float64 | Percentile rank of current σ vs trailing 24h |
| `hour_bucket` | UInt8 | Hour of day (0-23) |
| `btc_ret_1s` | Float64 | BTC log-return, 1s (cross-asset feature for ETH/SOL/etc.) |
| `resolution_distance` | Float64 | |spot − start| / start; only populated at TTE < 60s |
| `tte_s` | Float64 | Time to expiry in seconds |
| `ohanism_inventory` | Float64 | Net ohanism position in this token at fill time |
| `ohanism_total_exposure` | Float64 | Total dollar exposure across all tokens |
