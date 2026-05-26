# Schema Reference

## Storage Format

All raw data: **gzipped JSONL** (one JSON object per line), written atomically via `.tmp` → fsync → rename.
Pipeline output: **Parquet** with zstd-6 compression, strict Polars schema.

## Common Fields

| Field | Type | Notes |
|---|---|---|
| `t_recv_ns` | int64 | Nanoseconds UTC when collector received the event |
| `feed` | str | Source feed identifier |

## Timestamp Convention

- Storage: int64 nanoseconds UTC
- WS inbound: millisecond string → `int(ts) * 1_000_000`
- Logs: ISO-8601 string only

## Money Convention

- Storage: string (Decimal, no float)
- On-chain: `amount_raw` (uint256 string) + `amount_decimal` (6dp string)
- CLOB prices/sizes: already in human-readable units from API

## Schema files

See `src/pm_research/schemas/`:
- `envelope.py` — common base fields
- `polymarket.py` — all 7 WS message types + internal events
- `binance.py` — aggTrade / bookTicker / depth / kline
- `polygon.py` — V2 OrderFilled, OrdersMatched, CTF events, pUSD/USDC.e transfers

## Parquet Column Types

| Logical type | Parquet type |
|---|---|
| Money (Decimal) | `Decimal(38, 18)` |
| Timestamp ns | `Int64` (not Timestamp — avoids TZ ambiguity) |
| Address (0x…) | `Utf8` (lowercase) |
| Token ID (uint256) | `Utf8` (decimal string) |
| bytes32 | `Utf8` (hex string, no 0x prefix for compactness) |
