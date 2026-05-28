"""Phase 7: Paper twin match metrics against real ohanism.

Computes the Phase 7 acceptance metrics:
- Fill count by hour (±20% target)
- Maker:taker ratio (±5pp target)
- Win rate
- Realized PnL by hour (±10% target)
- Position trajectory Pearson correlation (>0.7 target)
- Per-market fill timing KS test

Implemented in Phase 7.
See METHODOLOGY.md Phase 7.3 for specification.
"""

from __future__ import annotations
