"""Feature engineering for Layers 1-5.

Computes all features in docs/FEATURE_DICTIONARY.md. Implemented in Phase 4+.
All features computed strictly before t_recv_ns of the fill (no lookahead).

Memory strategy: feature computation joins ohanism_fills (~20k rows/day) to
pre-aggregated spot/book windows. Does not materialize full day of pm_clob
in memory. Peak RAM: <2 GB for feature matrix.
"""

from __future__ import annotations
