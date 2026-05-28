"""Layer 5: Inverse Reinforcement Learning — objective recovery.

MaxEnt IRL (Ziebart et al. 2008) to recover the reward function R(state, action; ψ).
Implemented in Phase 6 only if Layers 2-4 leave systematic residuals.

See METHODOLOGY.md Phase 6.6 and notes/REFERENCES.md for specification.
"""

from __future__ import annotations
