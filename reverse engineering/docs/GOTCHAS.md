# GOTCHAS

Every known trap in this analysis, numbered for reference. Written verbatim
from §11 of MASTER_PROMPT.md.

---

1. **Sign confusion in OrderFilled.side** — verify, don't assume. `side` is a
   raw uint8 (0=BUY, 1=SELL per ABI) but whose perspective (maker-order vs
   taker) is undocumented. Determine empirically by reconciling both candidate
   position series against the public PnL (Phase 1.4). Getting it wrong
   silently corrupts every inventory and PnL number. #1 source of bugs.

2. **PM tokens come in pairs**. Buying Up at p and buying Down at (1-p) are
   equivalent positions. Normalize: always express position as "long Up"
   (long Down = short Up).

3. **size semantics**. `price_change.size` is new resting size (not delta).
   `size: "0"` = level removed. Larger than prior = new order arrived. Smaller
   = cancel ± fill. Disambiguate via `OrderFilled` matching.

4. **Strike-equal resolution**. If `S_T == S_0` (resolution-source precision),
   market resolves Up by spec ("greater than or equal"). Fair value at boundary
   is slightly above 0.5. Model this — near-expiry near-ATM is where the edge
   lives.

5. **NegRisk markets are a different game**. Filter `negRisk == false`
   everywhere. NegRisk markets live on a different probability simplex;
   including them breaks the σ formula.

6. **The 1s before resolution is its own regime**. TTE<1s is latency races,
   not fair value. Either exclude from σ fit or model separately.

7. **Two ohanism orders can fill in the same block**. Dedupe by
   `(block_hash, log_index)`, never by `(maker, taker, side, price)`.

8. **Binance kline_1m close lags by up to 1s**. Use `aggTrade` or `bookTicker`
   for sub-second spot; klines for vol estimation only. `bookTicker` rows have
   no event-time field — their only clock is `t_recv_ns` (live, reliable).

9. **pUSD wrap/unwrap looks like trading**. Filter through bridge-edge logic
   (`CollateralOnramp 0x93070a…` and `CollateralOfframp 0x29579…` addresses)
   before counting as capital flow.

10. **Survivorship in markets**. Fast-resolving high-vol markets are
    over-represented in fill counts. Weight stats by market-time, not fill
    count, when comparing regimes.

11. **σ implied inversion is undefined at p ∈ {0, 1}**. Skip those fills in σ
    extraction or use a regularized inversion (cap σ at σ_max = 10 per symbol).

12. **OLS coefficients are biased when σ candidates are highly collinear**.
    Compute condition number; if >30, use ridge or PCA-then-OLS for diagnostic
    Layer 1; the structural ML Layer 2 handles collinearity correctly via
    likelihood.

13. **Time-series CV must preserve order**. Never random k-fold on time series.
    Use expanding window or rolling window.

14. **GARCH fits are slow on full days of 100ms data**. Subsample to 1s grid
    for GARCH fit; evaluate at fill times.

15. **Maker fills can have taker == 0x...0 or zero-address sometimes in certain
    match types**. Handle gracefully; treat as anonymous counterparty.

16. **No block timestamp is recorded; polygon t_recv_ns is backfill-polluted**.
    The indexer stores only `block_number`/`block_hash`/`tx_hash`/`log_index`/
    `t_recv_ns`. Live (`eth_subscribe`) rows have usable `t_recv_ns`; backfilled
    rows have `t_recv_ns` = backfill wall-clock. Always derive `t_block_ns` via
    `eth_getBlockByNumber` (cache the map) and treat that as the on-chain clock.
    Flag any row where recorded `t_recv_ns` and derived block time differ by >10s
    as backfilled and exclude its `t_recv_ns` from timing.

17. **The strike is not in the metadata**. Derive `start_strike_price` from the
    spot feed at the market `startDate`. And: 5m/15m markets resolve on
    Chainlink, hourly on Binance, but you only record Binance — so for 5m/15m
    the Binance-derived strike/spot is a proxy. Treat the Chainlink↔Binance
    basis as a residual source and validate by reconstructing a sample of
    resolved 5m/15m outcomes from Binance vs the on-chain `ConditionResolution`.
    If ohanism resolves better than Binance can explain, they may consume
    Chainlink directly (an information edge you can flag but not fully replicate).

18. **Inferred-schema parquet stores nested fields as JSON strings**. `json.loads`
    them: pm_clob `price_changes`/`bids`/`asks`; pm_meta `event`/`market`;
    binance depth/kline nested arrays. And pm_meta `market.clobTokenIds` is
    double-encoded (a JSON string inside the already-JSON-decoded market dict) —
    parse twice. Gamma/pm_meta market fields are camelCase (`conditionId`,
    `clobTokenIds`, `endDate`, `startDate`, `negRisk`, `acceptingOrders`);
    pm_clob WS fields are snake_case (`asset_id`, `event_type`). Do not mix the
    conventions across feeds.

19. **builder/metadata are stored as bytes32 hex WITHOUT a 0x prefix**
    (e.g. `"00"*32`), while `order_hash`/`tx_hash`/`block_hash` carry the `0x`
    prefix. Normalize before comparing. A zero builder (`"00"*32`) means direct
    submission (no relay) — meaningful for §10.

20. **pm_clob `last_trade_price.side` = the BOOK LEVEL taken (maker's side),
    NOT the taker's action direction.** Verified Phase 1 empirically: for fills
    where ltp.asset_id == polygon fill token_id, `ltp.side` matches `ohanism_side`
    (the maker's side) 100% of the time. Interpretation:
    - `side='SELL'`: the ASK level was lifted (ohanism sold tokens, taker bought)
    - `side='BUY'`: the BID level was crossed (ohanism bought tokens, taker sold)
    This is the OPPOSITE of the data-api `activity.side` convention (which is the
    taker's action). Do NOT use pm_clob ltp.side as a taker-direction indicator.

21. **pm_clob covers only ~72% of on-chain ohanism fills.** Short-lived 5m markets
    (and some 15m markets) expire before the collector can subscribe. The collector
    receives a `new_market` WS event and subscribes, but if the market is active for
    only 5 minutes and the collector's discovery cycle is 30s, a market may expire
    before any `book`/`price_change`/`last_trade_price` events are captured.
    Consequence: 28% of fills have no pm_clob t_ws_ns match (fallback = t_block_ns)
    and no pm_clob-derived market metadata.
    **In Phase 4–6 regressions**: must check whether the missing 28% are
    systematically different from the covered 72% by:
    (a) asset — does pm_clob undercover a specific symbol?
    (b) horizon — are 5m markets more affected than 15m or 1h?
    (c) time-of-day — does coverage drop in low-activity hours when the collector
        may fall behind on subscriptions?
    If the 28% are NOT representative, regressions on the pm_clob-covered subset
    will have selection bias. Weight or correct accordingly.
