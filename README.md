# pm-research

Data collection, storage, and analysis platform for Polymarket short-dated crypto Up-Down markets.

## Overview

7 systemd services on 1× EC2 instance (Ubuntu 24.04):
- `pm-clob-collector` — Polymarket CLOB WebSocket
- `binance-collector` — Binance public WebSocket
- `polygon-indexer` — Polygon on-chain events (CTF Exchange V2, pUSD, USDC.e)
- `pm-metadata-snapshotter` — Gamma API market metadata
- `wallet-attribution` — operator wallet graph
- `pipeline-rotator` — JSONL.gz → Parquet → S3
- `heartbeat-watchdog` — monitoring

## Documentation

- `docs/VERIFIED_FACTS.md` — empirically verified contract addresses, wire formats, decimals
- `docs/SCHEMA.md` — storage schema reference
- `docs/RUNBOOK.md` — operational runbook
- `DECISIONS.md` — non-obvious technical decisions

## Setup

```bash
cp .env.example .env
# Fill in .env values
pip install -r requirements.txt
```

## Development

```bash
pytest
mypy --strict src/
ruff check src/
```

## Acceptance Gate

24h fill count for `gabagool22` must match their public profile exactly; PnL within ±0.1%.
