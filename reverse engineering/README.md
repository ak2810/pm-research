# Reverse Engineering @ohanism

Recover the exact trading algorithm of `@ohanism` (proxy wallet
`0x89b5cdaaa4866c1e738406712012a630b4078beb`) on Polymarket short-dated
crypto Up/Down markets (BTC/ETH/SOL/XRP/DOGE at 5m/15m/1h horizons).

## Methodology

Seven-phase cascade. See `METHODOLOGY.md` for the complete working bible.

| Phase | Name | Status |
|-------|------|--------|
| 0 | Bootstrap | ✓ |
| 1 | Data validation | - |
| 2 | Maker/taker decomposition | - |
| 3 | Order lifecycle reconstruction | - |
| 4 | Fair value modeling (Layers 1+2) | - |
| 5 | Pricing adjustments (Layer 3) | - |
| 6 | Microstructure alpha (Layers 4+5) | - |
| 7 | Replication + validation (Layers 6+7) | - |

## Compute topology

ALL computation runs locally (i7-11700K / 64 GB / RTX 3060). EC2 is a
data source only — never a compute node. See `METHODOLOGY.md §1`.

## How to run

```powershell
# Install dependencies (run once from this directory)
pip install -r requirements.txt

# Sync one hour of data from S3 to local cache
make sync

# Run precommit checks
make precommit

# Run phase-specific analysis
make phase1
make phase2
# ... etc.
```

## Key files

- `PROGRESS.md` — live progress tracker
- `RESULTS.md` — cumulative findings
- `DECISIONS.md` — technical decision log
- `ALGORITHM.md` — final extracted algorithm (written in Phase 7)
- `docs/GOTCHAS.md` — known traps and their mitigations
- `notes/VERIFIED_FACTS_RE.md` — empirically verified facts

## Working directory

`c:\users\avych\pm-research\reverse engineering\`
Python package: `reverse_engineering` (src layout under `src/`).
