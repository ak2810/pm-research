"""Phase 1: Cross-feed timestamp alignment checks.

Validates polygon vs pm_clob vs Binance clock consistency.
Flags backfilled polygon rows (t_recv_ns diverges from derived t_block_ns
by >10s — see GOTCHAS.md #16).

Implemented in Phase 1.
See METHODOLOGY.md Phase 1.2 for specification.
"""

from __future__ import annotations
