"""Build level_changes table: pm_clob price_change event classification.

Implemented in Phase 3 after ohanism_fills table is complete.
See METHODOLOGY.md Phase 3.1 and docs/SCHEMA.md for column specification.

Memory strategy: processes one market at a time (single token_id). Each
market's WS events for a day fit comfortably in RAM (<<1 GB).
"""

from __future__ import annotations
