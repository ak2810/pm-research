"""All σ candidate estimators for Phase 4.3.

Each estimator takes a series of Binance bookTicker mid log-returns
(100ms resolution, as a numpy array) and the timestamp of the evaluation
point, and returns an annualized volatility estimate.

Annualization convention: all σ values are annualized over calendar years
(252 trading days × 24 h × 3600 s = 31,536,000 seconds/year). Since crypto
trades 24/7, we use full-year seconds, not equity-market convention.

Memory strategy: estimators operate on pre-filtered numpy arrays (small
per-market slices). No full-day arrays materialized here.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from collections.abc import Sequence

_SECONDS_PER_YEAR: float = 365.0 * 24.0 * 3600.0
_DT_SECONDS: float = 0.1  # 100ms Binance tick
_ANN_FACTOR: float = math.sqrt(_SECONDS_PER_YEAR / _DT_SECONDS)


def sigma_rv(returns: npt.NDArray[np.float64], window_seconds: float) -> float:
    """Realized volatility over a trailing window.

    Args:
        returns: Array of 100ms log-returns, most-recent last.
        window_seconds: Lookback window in seconds.

    Returns:
        Annualized realized volatility. Returns 0.0 if fewer than 2 returns.
    """
    n = max(1, int(window_seconds / _DT_SECONDS))
    tail = returns[-n:] if len(returns) >= n else returns
    if len(tail) < 2:
        return 0.0
    variance = float(np.mean(tail**2))
    return math.sqrt(variance) * _ANN_FACTOR


def sigma_ewma(
    returns: npt.NDArray[np.float64],
    lam: float,
) -> float:
    """RiskMetrics EWMA annualized volatility.

    Recursion: h_t = λ * h_{t-1} + (1-λ) * r_t^2.
    Initializes with the variance of the full series.

    Args:
        returns: Array of 100ms log-returns.
        lam: Decay factor λ ∈ (0, 1).

    Returns:
        Annualized EWMA volatility.
    """
    if len(returns) < 2:
        return 0.0
    h = float(np.var(returns))
    for r in returns:
        h = lam * h + (1.0 - lam) * float(r) ** 2
    return math.sqrt(h) * _ANN_FACTOR


def sigma_seasonal(
    base_sigma: float,
    hour_factors: npt.NDArray[np.float64],
    hour: int,
) -> float:
    """Hour-of-day seasonally adjusted volatility.

    Args:
        base_sigma: Baseline 24h realized volatility (annualized).
        hour_factors: Array of 24 floats, mean-1 rescaling by hour.
                      hour_factors[h] = median(σ_rv_60s at hour h) /
                                        mean(median(σ_rv_60s) across all hours).
        hour: UTC hour at evaluation time (0-23).

    Returns:
        Seasonally adjusted annualized volatility.
    """
    if len(hour_factors) != 24:
        raise ValueError("hour_factors must have 24 entries (one per UTC hour)")
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    return base_sigma * float(hour_factors[hour])


def sigma_intraday_intensity(
    sigma_rv_60s: float,
    trade_count_last_60s: int,
    baseline_trade_count: float,
) -> float:
    """Event-time vol proxy: scale realized vol by trade-arrival density.

    σ_intraday = σ_rv_60s * sqrt(N_t / N_baseline)

    Args:
        sigma_rv_60s: Realized vol over last 60s (annualized).
        trade_count_last_60s: aggTrade count in last 60s.
        baseline_trade_count: Median aggTrade count per 60s over trailing day.

    Returns:
        Adjusted annualized volatility.
    """
    if baseline_trade_count <= 0:
        return sigma_rv_60s
    scale = math.sqrt(max(trade_count_last_60s, 1) / baseline_trade_count)
    return sigma_rv_60s * scale


def sigma_parkinson(
    highs: Sequence[float],
    lows: Sequence[float],
) -> float:
    """Parkinson range estimator from kline_1m candle high/low prices.

    σ_parkinson = sqrt( (1 / (4*n*ln(2))) * Σ [ln(H_i/L_i)]^2 ) * ann_factor

    Ann factor converts from per-candle (1 minute) to annual:
    sqrt(365 * 24 * 60).

    Args:
        highs: High prices for each candle.
        lows: Low prices for each candle.

    Returns:
        Annualized Parkinson volatility. Returns 0.0 if insufficient data.
    """
    if len(highs) != len(lows):
        raise ValueError("highs and lows must have equal length")
    if len(highs) < 1:
        return 0.0

    valid_terms: list[float] = []
    for h, lo in zip(highs, lows, strict=False):
        if h > 0 and lo > 0 and h >= lo:
            valid_terms.append(math.log(h / lo) ** 2)

    if not valid_terms:
        return 0.0

    n = len(valid_terms)
    variance_per_candle = sum(valid_terms) / (4.0 * n * math.log(2.0))
    ann_factor = math.sqrt(365.0 * 24.0 * 60.0)
    return math.sqrt(variance_per_candle) * ann_factor


def compute_ewma_series(
    returns: npt.NDArray[np.float64],
    lam: float,
) -> npt.NDArray[np.float64]:
    """Compute EWMA variance series h_t for each time step.

    Useful for evaluating σ_ewma at arbitrary historical points.

    Args:
        returns: Full time series of log-returns.
        lam: Decay factor λ.

    Returns:
        Array of EWMA variance estimates (same length as returns).
    """
    if len(returns) == 0:
        return np.array([], dtype=np.float64)

    h_series = np.empty(len(returns), dtype=np.float64)
    h: float = float(np.var(returns[:10])) if len(returns) >= 10 else float(returns[0] ** 2)
    for i, r in enumerate(returns):
        h = lam * h + (1.0 - lam) * float(r) ** 2
        h_series[i] = h
    return h_series
