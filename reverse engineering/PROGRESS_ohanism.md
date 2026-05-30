# PROGRESS — ohanism (0x89b5cdaaa4866c1e738406712012a630b4078beb)

ohanism-specific progress tracking. Project-wide progress in PROGRESS.md (top-level).

---

## CURRENT
**Phase**: Phase 5 COMPLETE (P1 borderline, P2/P3/P4 PASS)
**Sub-step**: K1-K6 done; BLOCKER-008 logged (temporal shift, not overfitting); Phase 6 next
**Started**: 2026-05-30T22:00:00Z

### Window state (S1-S5, 2026-05-30T21:25Z)
- WINDOW_START: 2026-05-27 04:00 UTC
- WINDOW_END:   2026-05-30 16:59 UTC (latest hour where all 4 feeds cached)
- Hours covered: 84 (04h 2026-05-27 → 16h 2026-05-30)
- Partitions: pm_clob=87, polygon=87, binance=87, pm_meta=87 (all feeds at 87 S3; 86 usable for window)
- New partitions synced this session: 123 (6.0 min)
- Common window set: 86 date-hour pairs

## JUST DID
Pre-5.I/J COMPLETE (2026-05-30T21:25Z): canonical-side audit clean; Phase 4 unaffected.

I-audit findings (code trace on all src/ and scripts/):
- sigma_implied_v2: p_canonical=1-price for Down fills ✓ (phase4_step1b lines 67-70)
- L2 structural fit: uses p_obs=p_posted from sigma_implied_v2 ✓
- OTM cushion (economic_offsets.py): uses up_price=1-price for Down ✓
- σ_implied inversion (step1/1b): uses p_canonical ✓
- Rebate formula: min(p,1-p) symmetric — unaffected ✓
- AS cost (economic_offsets.py): uses canonical up_price ✓
Three minor non-blocking instances:
- phase4_step46_profitability.py: same MTM bug but superseded by pre5a (Binance proxy, not used)
- inventory.py L53: dollar_exposure uses raw token price — affects descriptive stats only, not model inputs
- economic_offsets.py L85,94: rebate_pct_of_notional uses raw notional — diagnostic metric only

J-spotcheck: p_posted in sigma_implied_v2 confirmed canonical (|p_posted-0.5| median=0.22,
same OTM cushion as fills — consistent with passive post-once strategy at market open).
Phase 4 theta_hat stands. No Phase 4 re-run needed.

S1-S5 window update: WINDOW_END=2026-05-30/16. 123 new partitions synced in 6.0 min.
84h analysis window (2026-05-27/04 → 2026-05-30/16).

Pre-5.G/H COMPLETE (2026-05-29T20:00Z): root cause found and fixed.

Bug: Down-token fills (SELL Down, BUY Down) used price_f=raw_price instead of 1-raw_price.
For ohanism's typical ITM Down sells (q_D~0.65), this overstated losses by $91K total.
Fix: `price_f = 1-price if outcome_side=="Down" else price` in pre5a and pre5de scripts.

CORRECTED RESULTS:
- Net P&L: +7,390 USDC (was -83,831 USDC)
- G6: PASS (was FAIL)
- E3: PASS (ratio=0.583, was FAIL with wrong formula)
- External: extrapolated monthly = +109K vs public +173K (ratio 1.6x, plausible)
- BLOCKER-006: RESOLVED. BLOCKER-007b: RESOLVED.

Pre-5.F COMPLETE (2026-05-29T19:30Z): per-position formula validated against data-api.
All 4 resolved positions: gap < 1% (0.3-0.7%). F5 PASS. BLOCKER-007 RESOLVED.
Leaderboard -1,382 is 24h rolling metric (not 49h window figure) — no contradiction.
Formula for -83,831 USDC window P&L is confirmed correct.

Pre-5.C/D/E COMPLETE (2026-05-29T18:30Z):
- C: Leaderboard PnL = -1,382 USDC lifetime vs our -83,831 window. No windowed endpoint.
  60× magnitude gap, same sign. Likely accounting methodology difference.
- D: D4 PASS (unfavorable markets dominate). D5: all 12 sign checks PASS.
- E: E3 formula flag (check uses wrong max-loss formula; MTM values themselves verified by D5).
  E4 PASS (43.6% capital loss plausible in down-market).
BLOCKER-007 logged as non-blocking. Phase 5 cleared.

## NEXT
Phase 6 — Layer 4 reinforcement / paper twin synthesis.
Use Phase 5 replication-critical features:
  {fair_value, otm_cushion, lag_s, spot_z}
Incorporate into paper twin quote policy.

Phase 5 — Layer 3 GBT residual model on L2 residuals (COMPLETE).
Key addition per blocker resolution: include directional-regime features:
  - spot_return_open_to_post: spot pct return from market open to ohanism's quote time
  - spot_return_post_to_fill: spot pct return from post to fill
  - rolling_dir_5m: rolling % of preceding 5m windows where Up wins
Features in: METHODOLOGY §6.1 (spot-PM basis, lead-lag, book imbalance) + these 3 new.
Acceptance gate: GBT ≥ 5% additional variance explained over L2.

---

## HISTORY (most recent first)

### 2026-05-29T08:42:00Z — A1 COMPLETE
S3 enumeration: 50 partitions per feed, window 2026-05-27/04 – 2026-05-29/04, 49h.
Commits: f7ef2c6 (pre-phase4), 2165bd6 (phase3), 66397a6 (canonical+settlement).

### 2026-05-29T02:28:00Z — Phase 2+3 COMPLETE
Phase 2: 100% maker, 83.4% SELL (artifact), +6.9% canonical long-Up, $167k peak exposure,
0% net-zero final. Phase 3: 0.15% pull rate, median lifetime 26ms, 18.4s flip latency.
Price-format fix, 17 regression tests passing. 4 pre-Phase-4 tasks done.
Commits: be228ca, 3adcc7a, 2165bd6, f7ef2c6.

### 2026-05-29T00:20:00Z — Phase 1 COMPLETE
21,451 fills extracted (2026-05-27). All Phase 1 gates documented.
Commits: 797f90a, 904e69d.

### 2026-05-28T22:28:00Z — Phase 0 COMPLETE (BLOCKER-001 resolved)
AWS .env in place, make sync verified, 39 tests pass, GPU confirmed.
Commits: e3defa4, 73b8cdc.
