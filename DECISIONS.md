# Decisions

Record non-obvious technical decisions with rationale here.

---

## 2026-05-26

### Use tag_id=102127 for market discovery (not slug regex)
Two completely different slug patterns exist for 5m vs hourly markets. Tag-based discovery is schema-agnostic.

### Track pUSD AND USDC.e in wallet attribution
pUSD = current trading; USDC.e = wrap/unwrap bridge + pre-2026-04-28 history. ohanism active since Feb 2026.

### Store amounts as amount_raw + amount_decimal (both strings)
`amount_raw` = uint256 on-chain value as string; `amount_decimal` = Decimal(raw)/10^6 as 6dp string.
Avoids float precision loss; keeps full on-chain fidelity; Polars reads both cleanly.

### Parse book frames by shape, not event_type
Live capture confirmed: `book` frames have no `event_type`. Detected by presence of `bids` + `asks` keys.

### Normalize WS recv frames to list always
Server sends single object OR array per recv. `items = data if isinstance(data, list) else [data]` on every recv.
