# Verified Facts

All claims in this document were empirically verified in plan mode on 2026-05-26.
Do not accept spec assertions that conflict with this file — this file wins.

---

## Polygon Mainnet Contracts (chainId=137)

Source: Polygonscan `#code` tab + `verified badge` for each address. Cross-checked against `https://docs.polymarket.com/resources/contracts`.

| Contract | Address | Note |
|---|---|---|
| **CTF Exchange V2** | `0xE111180000d2663C0091e4f400237545B87B996B` | 52M+ tx; active exchange |
| **Neg Risk CTF Exchange V2** | `0xe2222d279d744050d28e00520010520000310F59` | 10.7M+ tx |
| **Neg Risk Adapter** | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` | |
| **Conditional Tokens (CTF)** | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | ERC-1155 outcome tokens |
| **pUSD (proxy)** | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | Current collateral post-2026-04-28 |
| **pUSD (impl)** | `0x6bBCef9f7ef3B6C592c99e0f206a0DE94Ad0925f` | |
| **CollateralOnramp (USDC.e→pUSD)** | `0x93070a847efEf7F70739046A929D47a521F5B8ee` | |
| **CollateralOfframp (pUSD→USDC.e)** | `0x2957922Eb93258b93368531d39fAcCA3B4dC5854` | |
| **USDC.e** | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | Pre-migration collateral + wrap/unwrap |
| **Polymarket Proxy Factory** | `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052` | Emits `ProxyCreated(address proxy, address signer)` |
| **Deposit Wallet Factory** | `0x00000000000Fb5C9ADea0298D729A0CB3823Cc07` | |
| **UMA Adapter** | `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` | Oracle for UMA-resolved markets |
| **UMA Optimistic Oracle** | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | |

**V1 exchange `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e` is DEPRECATED.** Do not index it for current operations. The original PROMPT.md spec referenced V1 — incorrect.

---

## Polymarket V2 Migration

- **Date**: 2026-04-28 ~11:00 UTC
- **Change**: CLOB collateral switched from USDC.e → pUSD. V2 exchange deployed.
- **Impact on indexer scope**:
  - Post-2026-04-28: trading events are pUSD transfers on CTF Exchange V2.
  - Pre-2026-04-28: trading events are USDC.e transfers on CTF Exchange V1 (out of scope for new fills, but needed for ohanism historical analysis).
  - Wrap/unwrap flows: `CollateralOnramp.wrap()` moves USDC.e → pUSD; `CollateralOfframp.unwrap()` reverses. Both legs emit ERC-20 Transfer events.

---

## V2 Exchange Event Signatures

### CTF Exchange V2 (`0xE111…`) and Neg Risk CTF Exchange V2 (`0xe222…`)
Both contracts share identical ABI (verified bytecode on Polygonscan).

```
OrderFilled(
    bytes32 indexed orderHash,
    address indexed maker,
    address indexed taker,
    uint8 side,             -- enum Side {BUY=0, SELL=1}
    uint256 tokenId,
    uint256 makerAmountFilled,
    uint256 takerAmountFilled,
    uint256 fee,
    bytes32 builder,        -- NEW in V2 (operator/builder tag)
    bytes32 metadata        -- NEW in V2 (arbitrary metadata)
)

OrdersMatched(
    bytes32 indexed takerOrderHash,
    address indexed takerOrderMaker,
    uint8 side,
    uint256 tokenId,
    uint256 makerAmountFilled,
    uint256 takerAmountFilled
)

OrderPreapproved(bytes32 indexed orderHash)
OrderPreapprovalInvalidated(bytes32 indexed orderHash)
FeeCharged(address indexed receiver, uint256 amount)
FeeReceiverUpdated(address indexed feeReceiver)
TradingPaused(address indexed pauser)
TradingUnpaused(address indexed pauser)
UserPaused(address indexed user, uint256 effectivePauseBlock)
UserUnpaused(address indexed user)
```

**Critical**: `OrderCancelled` does NOT exist in V2. Cancellations are off-chain only (CLOB WS user channel + REST API). The original PROMPT.md spec's expectation of on-chain cancellation events is wrong for V2.

### Conditional Tokens (`0x4D97…`)
```
TransferSingle(address indexed operator, address indexed from, address indexed to, uint256 id, uint256 value)
TransferBatch(address indexed operator, address indexed from, address indexed to, uint256[] ids, uint256[] values)
PositionSplit(address indexed stakeholder, address collateralToken, bytes32 indexed parentCollectionId, bytes32 indexed conditionId, uint256[] partition, uint256 amount)
PositionsMerge(address indexed stakeholder, address collateralToken, bytes32 indexed parentCollectionId, bytes32 indexed conditionId, uint256[] partition, uint256 amount)
PayoutRedemption(address indexed redeemer, address indexed collateralToken, bytes32 indexed parentCollectionId, bytes32 conditionId, uint256[] indexSets, uint256 payout)
ConditionPreparation(bytes32 indexed conditionId, address indexed oracle, bytes32 indexed questionId, uint256 outcomeSlotCount)
ConditionResolution(bytes32 indexed conditionId, address indexed oracle, bytes32 indexed questionId, uint256 outcomeSlotCount, uint256[] payoutNumerators)
```

---

## Decimal Precision

- **pUSD**: 6 decimals. Verified via live `eth_call` on 2026-05-26:
  - RPC: `https://1rpc.io/matic`
  - Call: `{"method":"eth_call","params":[{"to":"0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB","data":"0x313ce567"},"latest"]}`
  - Result: `0x0000000000000000000000000000000000000000000000000000000000000006` → uint8 = **6**
- **USDC.e**: 6 decimals.
- **CTF outcome tokens**: 6 decimals (same as collateral).
- **CLOB API**: returns human-readable sizes already divided by 10^6 (e.g. `"size": "16903.48"` = 16903.48 USDC).
- **All on-chain amounts**: stored as `amount_raw` (uint256 string) + `amount_decimal` (Decimal(raw) / 10^6 as string, 6 fixed decimal places).

---

## Polymarket CLOB Websocket

### Endpoints
```
Market channel: wss://ws-subscriptions-clob.polymarket.com/ws/market
User channel:   wss://ws-subscriptions-clob.polymarket.com/ws/user
```

### Subscribe message (market channel)
```json
{"assets_ids": ["<token_id>", ...], "type": "market", "custom_feature_enabled": true}
```
`custom_feature_enabled: true` enables `best_bid_ask`, `new_market`, `market_resolved` events.

### Wire format facts (empirically captured 2026-05-26, 64 frames over 60s)

**Frame envelope**: server sends EITHER a single JSON object OR a JSON array in one WebSocket frame. Parser must normalize:
```python
items = data if isinstance(data, list) else [data]
```

**`book` events have NO `event_type` field.** Detected by structural presence of `bids` + `asks` keys.

**Field naming**: snake_case throughout (`event_type`, `asset_id`, `condition_id`, `clob_token_ids`, `assets_ids`, `order_price_min_tick_size`, `event_message`, `taker_base_fee`, `fee_schedule`). Gamma REST API uses camelCase — different.

**All prices and sizes are JSON strings** — parse to `Decimal`, never `float`.

**All timestamps are millisecond strings** — convert to int64 ns: `int(ts_str) * 1_000_000`.

### Message schemas (live-verified)

#### `book` (initial snapshot, sent on subscribe and after gap recovery)
```json
{
  "market": "0x125730a9a19a6bc2d0f847f04f8bf16837484ca0131cf0fc79226075ecc50ebd",
  "asset_id": "41904046339315199441846846901798861215014557277009414496634485581318123208334",
  "timestamp": "1779782571250",
  "hash": "3c15c202a0d87c611d1af43fcc02098a770139db",
  "bids": [{"price": "0.01", "size": "16903.48"}, ...],
  "asks": [{"price": "0.50", "size": "1234.56"}, ...]
}
```
`size` is total resting size at that level. A level absent from the list has zero size.

#### `price_change` (order book delta)
```json
{
  "market": "0x125730a9a19a6bc2d0f847f04f8bf16837484ca0131cf0fc79226075ecc50ebd",
  "price_changes": [
    {
      "asset_id": "85820491405070503157833602237286422627039369979142976656076036182129921475920",
      "price": "0.03",
      "size": "23391.25",
      "side": "BUY",
      "hash": "789f4c2e63b0bc047478203856c749bef6bd8828",
      "best_bid": "0.5",
      "best_ask": "0.51"
    },
    {
      "asset_id": "41904046339315199441846846901798861215014557277009414496634485581318123208334",
      "price": "0.97",
      "size": "23391.25",
      "side": "SELL",
      "hash": "d74b36836a44e4495615d73236804c98c0157526",
      "best_bid": "0.49",
      "best_ask": "0.5"
    }
  ],
  "timestamp": "1779782572795",
  "event_type": "price_change"
}
```
`size` = new resting size at that price level (not a delta). `size: "0"` = level removed.
One `price_change` event typically covers both token IDs (Up + Down) of the same market in `price_changes[]`.

#### `new_market` (auto-discovery, requires `custom_feature_enabled: true`)
```json
{
  "id": "2359818",
  "question": "Bitcoin Up or Down - May 27, 3:55AM-4:00AM ET",
  "market": "0x525bc811d0ef8672e26e903371ebbfbe3a6d24bf4c55aac41ddd74499d09ffa3",
  "slug": "btc-updown-5m-1779868500",
  "description": "...",
  "assets_ids": ["83569573...", "79967973..."],
  "outcomes": ["Up", "Down"],
  "event_message": {
    "id": "526320",
    "ticker": "btc-updown-5m-1779868500",
    "slug": "btc-updown-5m-1779868500",
    "title": "Bitcoin Up or Down - May 27, 3:55AM-4:00AM ET",
    "description": "..."
  },
  "timestamp": "1779782572324",
  "event_type": "new_market",
  "tags": [],
  "condition_id": "0x525bc811d0ef8672e26e903371ebbfbe3a6d24bf4c55aac41ddd74499d09ffa3",
  "active": false,
  "clob_token_ids": ["83569573...", "79967973..."],
  "sports_market_type": "",
  "line": "",
  "game_start_time": "",
  "order_price_min_tick_size": "0.01",
  "group_item_title": "",
  "taker_base_fee": "1000",
  "fees_enabled": true,
  "fee_schedule": {"exponent": "1", "rate": "0.07", "taker_only": true, "rebate_rate": "0.2"}
}
```
`active: false` at creation. Do NOT auto-subscribe until `active: true` (poll Gamma or wait for state event).
`assets_ids` and `clob_token_ids` refer to the same array (two field names, same data).

#### Other message types (from docs; not observed in 60s window)
- `last_trade_price`: `{asset_id, event_type, fee_rate_bps, market, price, side, size, timestamp}`
- `tick_size_change`: `{event_type, asset_id, market, old_tick_size, new_tick_size, timestamp}`
- `best_bid_ask`: `{event_type, market, asset_id, best_bid, best_ask, spread, timestamp}`
- `market_resolved`: `new_market` shape + `winning_asset_id`, `winning_outcome`

### Internal collector events (not from server)
`subscribe_ack`, `disconnect`, `reconnect`, `heartbeat`, `market_closed` — generated by collector code, stored in same JSONL stream.

---

## Gamma API

Base: `https://gamma-api.polymarket.com`

| Endpoint | Purpose |
|---|---|
| `GET /events?tag_id=102127&closed=false&limit=N&offset=M` | Paginate all active Up-or-Down markets |
| `GET /events?slug=<slug>` | Single event lookup |
| `GET /events/slug/{slug}` | Path-based variant |
| `GET /series?slug=<series-slug>` | Series with child markets |
| `GET /public-profile?address=<0x…>` | Username/proxyWallet lookup |

**Field naming on `markets[]` objects: camelCase** (`conditionId`, `questionID`, `clobTokenIds`, `endDate`, `startDate`, `negRisk`, `orderPriceMinTickSize`, `feeSchedule`, `makerBaseFee`, `takerBaseFee`, `acceptingOrders`, `bestBid`, `bestAsk`, `lastTradePrice`). The original PROMPT.md listed snake_case — wrong.

`clobTokenIds` is a **JSON-string-of-array** in the API response (not a native array). Parse with `json.loads(market["clobTokenIds"])`.

---

## Data API

- `GET https://data-api.polymarket.com/v1/leaderboard?userName=<name>` — resolve username → proxyWallet.
  - Returns: `[{rank, proxyWallet, userName, xUsername, verifiedBadge, vol, pnl, profileImage}]`
- The endpoint `data-api.polymarket.com/profile?username=` returns **404** — does not exist. PROMPT.md was wrong.

---

## Operator Wallets (resolved 2026-05-26)

| Username | proxyWallet |
|---|---|
| `gabagool22` | `0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d` |
| `ohanism` | `0x89b5cdaaa4866c1e738406712012a630b4078beb` |

Signer EOA behind each proxy: resolve at build time via `ProxyCreated(address proxy, address signer)` event on `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052`.

---

## Short-Dated Crypto Up-Down Markets

### 5-Minute Markets
- Slug: `{asset}-updown-5m-{unix_start}` (e.g. `btc-updown-5m-1779783300`)
- Question: `"{Asset} Up or Down - {Mon} {D}, {H}:{MM}{AM/PM}-{H}:{MM}{AM/PM} ET"`
- Resolution: Chainlink BTC/USD data stream (`https://data.chain.link/streams/btc-usd`)
- `negRisk: false` → settles via **CTF Exchange V2** (`0xE111…`)
- Tick size: 0.01, min order: 5 USDC
- Fee: `{rate: 0.07, takerOnly: true, rebateRate: 0.2, exponent: 1}`

### Hourly Markets
- Slug: `{asset_full}-up-or-down-{month}-{day}-{year}-{hour}{am|pm}-et`
- Series slug: `{asset}-up-or-down-hourly`
- Question: `"{Asset} Up or Down - {Mon} {D}, {H}{AM/PM} ET"`
- Resolution: **Binance `{ASSET}/USDT` 1-hour candle** (different from 5m!)
- `negRisk: false` → settles via CTF Exchange V2
- Assets: BTC, ETH, SOL, XRP, DOGE (DOGE was not in PROMPT.md spec)

### Non-Existent Variants
**1-minute and 15-minute markets do NOT currently exist** (verified via tag 102127 enumeration + series-slug probe on 2026-05-26). Collector enumerates whatever tag 102127 returns without hardcoding these horizons.

### Discovery Method
Poll `GET /events?tag_id=102127&closed=false` every 30s with pagination.
Filter: `negRisk == false`, `acceptingOrders == true`, asset in allowed list, `endDate` within next 6h.
Also subscribe to `new_market` WS events (between polls) via `custom_feature_enabled: true`.

---

## Binance

- WS combined stream: `wss://stream.binance.com:9443/stream?streams=...`
- Stream names: `<sym>@aggTrade`, `<sym>@bookTicker`, `<sym>@depth@100ms`, `<sym>@kline_1m`
- Max connection lifetime: 24h → pre-emptive reconnect at 23h with 10s overlap + dedup on `(stream, E)`
- Local book management: snapshot → buffer deltas → apply buffered deltas after snapshot received

---

## Wallet Attribution: Dual-Token Requirement

Must track BOTH tokens for complete attribution:

1. **pUSD** (`0xC011…2DFB`): all current (post-2026-04-28) trading-related transfers
2. **USDC.e** (`0x2791…4174`): wrap/unwrap bridge events + pre-migration history

Bridge edges:
- USDC.e → CollateralOnramp (`0x93070a…`) = wrap start
- pUSD ← CollateralOnramp = wrap end
- pUSD → CollateralOfframp (`0x29579…`) = unwrap start
- USDC.e ← CollateralOfframp = unwrap end

Both legs of a wrap/unwrap must be linked as the same logical funding event in `wallet_graph.json`.

`ohanism` has history from ~Feb 2026 (entirely in USDC.e). Without USDC.e backfill, those flows are invisible.

---

## Build-Time Open Items (non-schema-critical, defer to build)

1. **Oracle address** for short-dated crypto market resolution: read `ConditionPreparation.oracle` from one resolved 5m and one resolved hourly market.
2. **CLOB REST endpoint for book snapshot on reconnect**: verify `GET /book?token_id=…` against py-clob-client source.
3. **Signer EOA** behind each operator proxy: derive from `ProxyCreated` event at backfill time.

---

## Spec Divergences from PROMPT.md

The original `docs/PROMPT.md` was written for Polymarket V1. These corrections apply:

| # | PROMPT.md (wrong) | Correct |
|---|---|---|
| 1 | CTF Exchange `0x4bfb…` | V2: `0xE111…` |
| 2 | Collateral = USDC.e | pUSD post-2026-04-28; both tracked |
| 3 | V2 OrderFilled missing builder/metadata | Must capture both bytes32 fields |
| 4 | `OrderCancelled` on-chain | No such event in V2; off-chain only |
| 5 | Gamma fields snake_case | camelCase (`conditionId`, `clobTokenIds`) |
| 6 | `data-api/profile?username=` | Use `/v1/leaderboard?userName=` |
| 7 | 1m/15m markets exist | Only 5m and 1h exist currently |
| 8 | Hourly resolves via Chainlink | Resolves via Binance 1h candle |
| 9 | Discovery via slug regex | Use tag_id=102127 enumeration |
