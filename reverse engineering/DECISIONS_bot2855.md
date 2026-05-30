# DECISIONS — bot2855 (0x2855555a48ee7ec2e67272701651bfe77034ebe8)

Bot2855-specific technical decisions. Template initialized from B1-B2 sanity checks.
Project-wide decisions in top-level DECISIONS.md.

---

## 2026-05-30 — B2: Initial characterization from public metadata

**Account created**: 2026-03-28 (2 months of trading history as of analysis start)
**Username**: None (anonymous — address displayed as name)
**EOA signer**: Not yet resolved (requires ProxyFactory ProxyCreated event lookup)

**Activity profile from data-api**:
- Market type: BTC Up/Down 5m (identical universe to ohanism)
- Fill rate: ~440-500 fills/hr (comparable to ohanism)
- Builder pattern: 99.6% direct submission (builder=zeros) — same as ohanism

**Collection status** (B1): 13,042 fills in last 30 partitions. Collection verified.

**Preliminary strategy class (from public metadata only, pre-analysis)**:
- Same asset/horizon focus as ohanism: crypto Up/Down 5m markets
- Direct submission (no relay) — operationally similar to ohanism
- 2-month trading history — more established than ohanism at analysis start
- Strategy class: UNKNOWN until Phase 1-3 analysis — could be passive MM,
  active MM, taker, or mixed. No conclusions drawn before analysis.

**Analysis hold**: Phase 1 analysis pending explicit user authorization.

**Date**: 2026-05-30
