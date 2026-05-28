"""Build the ohanism_fills table from polygon + pm_clob + pm_meta local cache.

This module is implemented in Phase 2 after data validation (Phase 1) passes.
See METHODOLOGY.md Phase 2 and docs/SCHEMA.md for the full column specification.

Memory strategy: scans polygon Parquet lazily, filters to ohanism fills, then
enriches per-hour against pm_clob and pm_meta. Processes one hour at a time.
Peak RAM: dominated by the polygon scan slice (~200k rows/hour × 30 cols).
"""

from __future__ import annotations

from typing import Final

OHANISM_PROXY: Final[str] = "0x89b5cdaaa4866c1e738406712012a630b4078beb"
CTF_V2: Final[str] = "0xe111180000d2663c0091e4f400237545b87b996b"
NEG_RISK_V2: Final[str] = "0xe2222d279d744050d28e00520010520000310f59"
