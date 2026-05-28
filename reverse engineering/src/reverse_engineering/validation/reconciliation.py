"""Phase 1: Fill count and PnL reconciliation against ohanism's public profile.

Compares fills extracted from local Parquet cache against
data-api.polymarket.com leaderboard for a fixed 24h window.

Implemented in Phase 1.
See METHODOLOGY.md Phase 1.1 for specification.
"""

from __future__ import annotations
