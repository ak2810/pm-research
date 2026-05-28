"""Closed-form fair value and implied-σ inversion for Up/Down digital options.

Up/Down markets are binary digital options:
  P(Up) = 1 - Φ(d),  d = log(S_0 / S_t) / (σ * sqrt(τ))

where:
  S_0 = start_strike_price (spot at market open)
  S_t = current spot
  τ   = time_to_expiry in years
  σ   = annualized volatility (annualized over years)

Strike-equal resolution (gotcha #4): if S_T == S_0, resolves Up by spec.
Model this by treating P(S_T >= S_0) = 1 - Φ(d) correctly (Φ is symmetric,
so no adjustment needed for continuous prices; the gotcha is numerical in
practice at very small τ).

Implied-σ inversion uses Brent's method bounded to (1e-6, 10).
Undefined at p ∈ {0, 1} (gotcha #11) — returns None for out-of-range prices.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from scipy import optimize
from scipy.stats import norm

if TYPE_CHECKING:
    from decimal import Decimal

_SIGMA_MIN: float = 1e-6
_SIGMA_MAX: float = 10.0
_PRICE_EPS: float = 1e-6  # prices closer than this to 0 or 1 are excluded


def fair_value_up(
    s0: Decimal,
    st: Decimal,
    tau_years: float,
    sigma: float,
) -> float:
    """GBM digital call fair value P(Up) = 1 - Φ(d).

    Args:
        s0: Start strike price (spot at market open).
        st: Current spot price.
        tau_years: Time to expiry in years (must be > 0).
        sigma: Annualized volatility (must be > 0).

    Returns:
        Probability in (0, 1).

    Raises:
        ValueError: If inputs are non-positive or tau/sigma are zero.
    """
    if float(s0) <= 0:
        raise ValueError(f"s0 must be positive, got {s0}")
    if float(st) <= 0:
        raise ValueError(f"st must be positive, got {st}")
    if tau_years <= 0:
        raise ValueError(f"tau_years must be positive, got {tau_years}")
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")

    d = math.log(float(s0) / float(st)) / (sigma * math.sqrt(tau_years))
    return float(1.0 - norm.cdf(d))


def invert_sigma(
    price: Decimal,
    s0: Decimal,
    st: Decimal,
    tau_years: float,
) -> float | None:
    """Invert the digital formula to recover implied σ from an observed price.

    Uses Brent's method on the interval (_SIGMA_MIN, _SIGMA_MAX).

    Args:
        price: Observed fill price (probability of Up).
        s0: Start strike price.
        st: Current spot.
        tau_years: Time to expiry in years (must be > 0).

    Returns:
        Implied σ if inversion succeeds, None if price is at boundary
        (gotcha #11: skip p ∈ {0, 1}) or inversion fails to converge.
    """
    p = float(price)
    if p <= _PRICE_EPS or p >= 1.0 - _PRICE_EPS:
        return None
    if tau_years <= 0:
        return None
    if float(s0) <= 0 or float(st) <= 0:
        return None
    if abs(math.log(float(s0) / float(st))) < 1e-10:
        return None

    def objective(sigma: float) -> float:
        return fair_value_up(s0, st, tau_years, sigma) - p

    f_lo = objective(_SIGMA_MIN)
    f_hi = objective(_SIGMA_MAX)

    if f_lo * f_hi > 0:
        return None

    try:
        result = optimize.brentq(
            objective,
            _SIGMA_MIN,
            _SIGMA_MAX,
            xtol=1e-9,
            rtol=1e-9,
            maxiter=200,
            full_output=True,
        )
        sigma_hat: float = result[0]
        converged: bool = result[1].converged
        if not converged:
            return None
        return sigma_hat
    except ValueError:
        return None


def invert_sigma_batch(
    prices: list[Decimal],
    s0: Decimal,
    st_series: list[Decimal],
    tau_series: list[float],
) -> list[float | None]:
    """Vectorized implied-σ inversion for a list of fills.

    Args:
        prices: Observed fill prices.
        s0: Start strike (same for all fills in a given market).
        st_series: Current spot at each fill time.
        tau_series: Time to expiry (years) at each fill time.

    Returns:
        List of implied σ values (None where inversion failed).
    """
    if not (len(prices) == len(st_series) == len(tau_series)):
        raise ValueError("prices, st_series, tau_series must have equal length")
    return [
        invert_sigma(p, s0, st, tau)
        for p, st, tau in zip(prices, st_series, tau_series, strict=False)
    ]
