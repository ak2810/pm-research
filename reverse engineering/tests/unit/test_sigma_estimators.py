"""Unit tests for sigma_estimators.py."""

from __future__ import annotations

import numpy as np
import pytest

from reverse_engineering.models.sigma_estimators import (
    compute_ewma_series,
    sigma_ewma,
    sigma_intraday_intensity,
    sigma_parkinson,
    sigma_rv,
    sigma_seasonal,
)


class TestSigmaRv:
    def test_zero_returns_gives_zero_vol(self) -> None:
        returns = np.zeros(100, dtype=np.float64)
        assert sigma_rv(returns, window_seconds=10.0) == 0.0

    def test_constant_returns_gives_positive_vol(self) -> None:
        returns = np.full(100, 0.001, dtype=np.float64)
        vol = sigma_rv(returns, window_seconds=10.0)
        assert vol > 0.0

    def test_fewer_than_2_returns(self) -> None:
        assert sigma_rv(np.array([0.001], dtype=np.float64), 1.0) == 0.0
        assert sigma_rv(np.array([], dtype=np.float64), 1.0) == 0.0

    def test_window_shorter_than_series_uses_tail(self) -> None:
        rng = np.random.default_rng(42)
        returns_long = rng.normal(0, 0.001, size=1000).astype(np.float64)
        returns_short = returns_long[-10:]
        sigma_rv(returns_long, window_seconds=100.0)
        vol_short_window = sigma_rv(returns_long, window_seconds=1.0)
        vol_reference = sigma_rv(returns_short, window_seconds=1.0)
        assert abs(vol_short_window - vol_reference) < 1e-10

    def test_annualization_consistent(self) -> None:
        """Doubling the per-step return should double the vol."""
        returns1 = np.full(100, 0.001, dtype=np.float64)
        returns2 = np.full(100, 0.002, dtype=np.float64)
        vol1 = sigma_rv(returns1, window_seconds=10.0)
        vol2 = sigma_rv(returns2, window_seconds=10.0)
        assert abs(vol2 / vol1 - 2.0) < 1e-9


class TestSigmaEwma:
    def test_zero_returns(self) -> None:
        returns = np.zeros(50, dtype=np.float64)
        assert sigma_ewma(returns, lam=0.97) == 0.0

    def test_positive_vol(self) -> None:
        rng = np.random.default_rng(0)
        returns = rng.normal(0, 0.002, size=500).astype(np.float64)
        vol = sigma_ewma(returns, lam=0.97)
        assert vol > 0.0

    def test_shorter_series(self) -> None:
        assert sigma_ewma(np.array([0.001], dtype=np.float64), lam=0.97) == 0.0

    def test_higher_lambda_smoother(self) -> None:
        """Higher λ = more weight on history → less responsive to spikes."""
        rng = np.random.default_rng(1)
        base = rng.normal(0, 0.001, size=100).astype(np.float64)
        spike = np.concatenate([base, np.array([0.1, 0.1], dtype=np.float64)])
        vol_94 = sigma_ewma(spike, lam=0.94)
        vol_99 = sigma_ewma(spike, lam=0.99)
        assert vol_94 > vol_99


class TestSigmaSeasonal:
    def test_basic(self) -> None:
        factors = np.ones(24, dtype=np.float64)
        assert sigma_seasonal(0.5, factors, 12) == pytest.approx(0.5)

    def test_rescaling(self) -> None:
        factors = np.ones(24, dtype=np.float64)
        factors[9] = 2.0
        assert sigma_seasonal(0.3, factors, 9) == pytest.approx(0.6)

    def test_invalid_hour(self) -> None:
        factors = np.ones(24, dtype=np.float64)
        with pytest.raises(ValueError, match="0-23"):
            sigma_seasonal(0.5, factors, 24)

    def test_wrong_factor_length(self) -> None:
        with pytest.raises(ValueError, match="24 entries"):
            sigma_seasonal(0.5, np.ones(12, dtype=np.float64), 0)


class TestSigmaIntradayIntensity:
    def test_baseline_equal_gives_same_vol(self) -> None:
        vol = sigma_intraday_intensity(0.4, trade_count_last_60s=100, baseline_trade_count=100.0)
        assert vol == pytest.approx(0.4)

    def test_higher_count_increases_vol(self) -> None:
        vol = sigma_intraday_intensity(0.4, trade_count_last_60s=400, baseline_trade_count=100.0)
        assert vol == pytest.approx(0.8)

    def test_zero_baseline_returns_input(self) -> None:
        assert sigma_intraday_intensity(0.3, 50, 0.0) == 0.3


class TestSigmaParkinson:
    def test_equal_high_low_gives_zero(self) -> None:
        vol = sigma_parkinson([100.0, 100.0], [100.0, 100.0])
        assert vol == 0.0

    def test_basic_positive(self) -> None:
        vol = sigma_parkinson([102.0, 103.0, 101.0], [98.0, 97.0, 99.0])
        assert vol > 0.0

    def test_empty_gives_zero(self) -> None:
        assert sigma_parkinson([], []) == 0.0

    def test_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            sigma_parkinson([100.0], [98.0, 99.0])

    def test_annualized_positive(self) -> None:
        highs = [50100.0] * 15
        lows = [49900.0] * 15
        vol = sigma_parkinson(highs, lows)
        assert vol > 0.0


class TestComputeEwmaSeries:
    def test_length_preserved(self) -> None:
        returns = np.random.default_rng(42).normal(0, 0.001, 200).astype(np.float64)
        series = compute_ewma_series(returns, lam=0.97)
        assert len(series) == len(returns)

    def test_empty_returns_empty(self) -> None:
        result = compute_ewma_series(np.array([], dtype=np.float64), lam=0.97)
        assert len(result) == 0

    def test_all_positive(self) -> None:
        returns = np.random.default_rng(0).normal(0, 0.001, 100).astype(np.float64)
        series = compute_ewma_series(returns, lam=0.97)
        assert np.all(series >= 0)
