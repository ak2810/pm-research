# PM CLOB Fixtures — 2026-05-26 BTC 5m

Live capture: 60s against `wss://ws-subscriptions-clob.polymarket.com/ws/market`
Market: `btc-updown-5m-1779783300` (BTC 5m, end 08:20 UTC)
Token IDs: `85820491…5920` (Up), `41904046…8334` (Down)
Subscribe: `{"assets_ids": [...], "type": "market", "custom_feature_enabled": true}`

## Frame counts
- Total frames: 64
- book: 2 (one per asset_id on subscribe)
- new_market: 4 (auto-discovery via custom_feature_enabled)
- price_change: 59

## Key observations (see docs/VERIFIED_FACTS.md)
- `book` frames have NO `event_type` field — detected by presence of `bids`+`asks`
- Server sends single object OR array per recv — normalize with `items = data if isinstance(data, list) else [data]`
- All prices/sizes are JSON strings — parse to Decimal, never float
- Timestamps are millisecond strings — convert via `int(ts) * 1_000_000`
- `new_market.active = false` at creation — do NOT auto-subscribe
