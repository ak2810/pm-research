"""Layer 6: Online adaptive replication — sliding-window structural refit.

Re-fits Layer 2 structural estimation on a 24h sliding window, hourly.
Tracks θ̂ drift over time. Implemented in Phase 7.

See METHODOLOGY.md Phase 7.1 for specification.
"""

from __future__ import annotations
