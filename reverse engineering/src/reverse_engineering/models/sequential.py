"""Layer 4: Sequential/state-dependent model (LSTM + Transformer).

Implemented in Phase 6 only if Layer 3 residuals are autocorrelated.
Uses PyTorch with CUDA (RTX 3060, 12 GB VRAM, cu124 wheel).

See METHODOLOGY.md Phase 6.5 for specification.
"""

from __future__ import annotations
