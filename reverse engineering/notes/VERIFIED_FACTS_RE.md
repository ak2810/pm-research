# VERIFIED FACTS — Reverse Engineering

Facts empirically verified during the reverse-engineering project. Inherits
all entries from `docs/VERIFIED_FACTS.md` (parent project). This file adds
new facts discovered during analysis phases.

Format per entry:
- **Fact**: what was verified
- **Source**: URL, RPC call, or empirical method
- **Date**: YYYY-MM-DD
- **Evidence**: captured output or calculation

---

## From parent project (inherited, do not duplicate)

See `c:\users\avych\pm-research\docs\VERIFIED_FACTS.md` for:
- All contract addresses (CTF V2, Neg Risk V2, CTF tokens, pUSD, etc.)
- Event signatures and topic0 hashes
- Fee schedule (rate=0.07, taker_only=true, rebate_rate=0.2, exponent=1)
- ohanism proxy wallet: 0x89b5cdaaa4866c1e738406712012a630b4078beb
- V2 migration date: 2026-04-28
- WS endpoints, Gamma API, Data API endpoints
- 5m/15m resolution source: Chainlink; hourly: Binance 1h candle
- NegRisk=false for short-dated crypto markets → settles on CTF Exchange V2

---

## Phase 0 — Environment

**Fact**: CUDA 13.1 driver (version 591.86) is present on the local RTX 3060.
PyTorch cu124 wheel (CUDA 12.4) is backward-compatible with this driver.
**Source**: `nvidia-smi` output on 2026-05-28.
**Date**: 2026-05-28
**Evidence**: `nvidia-smi` shows `Driver Version: 591.86 CUDA Version: 13.1`.

---

**Fact**: Standard LightGBM pip wheel does NOT include GPU (OpenCL) support
on Windows. GPU requires building from source with `-DUSE_GPU=ON`.
**Source**: LightGBM installation guide
(https://lightgbm.readthedocs.io/en/latest/Installation-Guide.html), confirmed
2026-05-28.
**Date**: 2026-05-28
**Evidence**: Install guide states Windows pip wheel is CPU-only; GPU build
requires Boost + CMake + `-DUSE_GPU=ON`.

---

---

**Fact**: EC2 .env file path is `/etc/pm-research/.env` (mode 0600, owned by root).
Non-secret values confirmed: `S3_BUCKET=pm-research-data`,
`AWS_DEFAULT_REGION=eu-west-1`.
**Source**: `sudo cat /etc/pm-research/.env | grep -E '^(S3_BUCKET|AWS_DEFAULT_REGION)='`
on ubuntu@34.244.229.19 via SSH.
**Date**: 2026-05-29

---

**Fact**: Local Polars 0.20.31 `scan_parquet` raises `DuplicateError` with
"invalid Hive partition schema" when reading files from Hive-partitioned paths
(e.g. `feed=X/date=Y/hour=Z/data.parquet`) unless `hive_partitioning=False`
is passed. Fix applied to local_reader.py and integration tests.
**Source**: Empirical — observed error 2026-05-29.
**Date**: 2026-05-29

---

---

**Fact**: ohanism `OrderFilled` price formula (empirically verified against hour=21, 747 fills):
- `side=0` (taker BUY): `price = maker_amount_decimal / taker_amount_decimal`; all 670 side=0 rows give price ∈ [0.04, 0.97].
  maker_amount = USDC received by ohanism (the maker), taker_amount = tokens delivered to taker.
  ohanism_side = SELL (ohanism sold tokens, received USDC).
  size = taker_amount_decimal (token quantity).
- `side=1` (taker SELL): `price = taker_amount_decimal / maker_amount_decimal`; all 77 side=1 rows give price ∈ [0.02, 0.95].
  maker_amount = tokens received by ohanism, taker_amount = USDC paid by ohanism.
  ohanism_side = BUY (ohanism bought tokens, paid USDC).
  size = maker_amount_decimal (token quantity).
- `fee_decimal = '0'` for ALL maker fills (confirmed, 747/747 = 0).
- `builder = '0' * 64` for ALL fills → direct submission, no relay.
- ohanism is ALWAYS maker in all 747 fills in the cached hour (0 taker fills).
- All fills on CTF Exchange V2 (`0xe111...`); 0 on Neg Risk V2.
**Source**: Empirical price range check — `formula_A_in_0_1=670/670` for side=0,
`formula_B_in_0_1=77/77` for side=1. Local polygon parquet, date=2026-05-28 hour=21.
**Date**: 2026-05-29

---

*(Further Phase 1+ facts appended as analysis proceeds.)*
