# Runbook

## Services

| Unit | Description |
|---|---|
| `pm-clob-collector` | Polymarket CLOB WS → JSONL.gz |
| `binance-collector` | Binance public WS → JSONL.gz |
| `polygon-indexer` | Polygon on-chain events → JSONL.gz |
| `pm-metadata-snapshotter` | Gamma API metadata → JSONL.gz (5 min timer) |
| `wallet-attribution` | Wallet graph builder (daily timer) |
| `pipeline-rotator` | JSONL.gz → Parquet → S3 (5 min timer) |
| `heartbeat-watchdog` | Monitors all 6 above; Discord alert on failure |

## Common Operations

### Restart a service
```bash
sudo systemctl restart pm-clob-collector
sudo journalctl -u pm-clob-collector -f
```

### Check health
```bash
sudo systemctl status pm-*
```

### Force pipeline rotation
```bash
sudo systemctl start pipeline-rotator
```

## Storage Layout

```
/var/pm-research/
  data/
    pm_clob/          # JSONL.gz by hour
    binance/
    polygon/
    pm_meta/
  state/
    polygon_indexer.cursor   # last processed block
  logs/                      # structured JSON logs
```

## S3 Layout

```
s3://{S3_BUCKET}/
  raw/
    feed=pm_clob/date=YYYY-MM-DD/hour=HH/data.parquet
    feed=binance/...
    feed=polygon/...
    feed=pm_meta/...
  wallet/
    wallet_graph.json
```

## Acceptance Gate

Run `pytest tests/acceptance/` after 24h of live data collection.
Primary gate: `gabagool22` 24h fill count exact match; PnL within ±0.1%.
