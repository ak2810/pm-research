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

*(Phase 1+ facts appended as analysis proceeds.)*
