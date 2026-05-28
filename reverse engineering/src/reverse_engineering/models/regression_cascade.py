"""Layer 1: Regression cascade diagnostic.

OLS regression of σ_implied on candidate σ estimators with HAC/Newey-West
standard errors. Implemented in Phase 4.4.

See METHODOLOGY.md Phase 4.4 for specification.
"""

from __future__ import annotations
