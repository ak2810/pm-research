# RESULTS

Cumulative findings, updated per phase. Each phase adds a section.

---

## Phase 0 — Bootstrap

**Environment**:
- Python: 3.12.6
- GPU: NVIDIA GeForce RTX 3060, 12 GB VRAM
- CUDA driver: 591.86, CUDA API: 13.1
- PyTorch wheel: cu124 (CUDA 12.4, backward-compatible with CUDA 13.1 driver)
- `torch.cuda.is_available()`: TBD — update after install
- LightGBM GPU: CPU-only (standard pip wheel; GPU requires source build on
  Windows — acceptable for Layer 3 given small dataset size)

**GPU confirmed**:
- `torch.cuda.is_available()`: True
- Device: `NVIDIA GeForce RTX 3060`
- PyTorch wheel: 2.6.0+cu124 (CUDA 12.4, backward-compatible with CUDA 13.1 driver)

**LightGBM GPU path**:
- `device="gpu"` on standard pip wheel (4.6.0): appears to fall back to CPU silently
  (multiple "1 warning generated" messages; no exception thrown)
- CPU fallback is acceptable per DECISIONS.md (Layer 3 dataset is tiny)

**S3 sync test** (make sync):
- BLOCKED — see BLOCKERS.md BLOCKER-001. Local .env file missing.
- All other Phase 0 items complete; sync to be run after .env created.

**EC2 health check**:
- pm-clob-collector status: `active` (SSH to ubuntu@34.244.229.19 succeeded)
- Key path: C:/Users/avych/pm-research-key.pem (confirmed reachable)

---

(Phase 1+ results appended after each phase completes.)
