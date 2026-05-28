"""Unit tests for digital_option.py — fair value and implied-σ inversion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from reverse_engineering.models.digital_option import (
    fair_value_up,
    invert_sigma,
    invert_sigma_batch,
)


class TestFairValueUp:
    def test_at_the_money_half(self) -> None:
        """ATM (S_0 == S_t) → d=0 → P(Up) = 0.5."""
        p = fair_value_up(
            s0=Decimal("50000"),
            st=Decimal("50000"),
            tau_years=1.0 / (365 * 24 * 12),  # 5 minutes
            sigma=0.5,
        )
        assert abs(p - 0.5) < 1e-9

    def test_spot_below_strike_reduces_up_prob(self) -> None:
        """S_t < S_0 means price must recover to beat strike → P(Up) < 0.5.

        Under GBM: P(Up) = 1 - Phi(log(S_0/S_t) / (sigma*sqrt(tau))).
        S_t < S_0 → log(S_0/S_t) > 0 → d > 0 → P(Up) < 0.5.
        """
        p = fair_value_up(
            s0=Decimal("50000"),
            st=Decimal("49000"),
            tau_years=5.0 / (365 * 24 * 60),  # 5 minutes
            sigma=0.5,
        )
        assert p < 0.5

    def test_spot_above_strike_raises_up_prob(self) -> None:
        """S_t > S_0 means price is already above strike → P(Up) > 0.5.

        S_t > S_0 → log(S_0/S_t) < 0 → d < 0 → P(Up) = 1 - Phi(negative) > 0.5.
        """
        p = fair_value_up(
            s0=Decimal("50000"),
            st=Decimal("51000"),
            tau_years=5.0 / (365 * 24 * 60),
            sigma=0.5,
        )
        assert p > 0.5

    def test_zero_tte_raises(self) -> None:
        with pytest.raises(ValueError, match="tau_years"):
            fair_value_up(
                s0=Decimal("50000"),
                st=Decimal("50000"),
                tau_years=0.0,
                sigma=0.5,
            )

    def test_zero_sigma_raises(self) -> None:
        with pytest.raises(ValueError, match="sigma"):
            fair_value_up(
                s0=Decimal("50000"),
                st=Decimal("50000"),
                tau_years=0.01,
                sigma=0.0,
            )

    def test_negative_s0_raises(self) -> None:
        with pytest.raises(ValueError, match="s0"):
            fair_value_up(
                s0=Decimal("-1"),
                st=Decimal("50000"),
                tau_years=0.01,
                sigma=0.5,
            )

    def test_probability_in_unit_interval(self) -> None:
        for sigma in (0.1, 0.5, 1.0, 3.0):
            p = fair_value_up(
                s0=Decimal("30000"),
                st=Decimal("31000"),
                tau_years=1.0 / (365 * 24 * 12),
                sigma=sigma,
            )
            assert 0.0 <= p <= 1.0


class TestInvertSigma:
    def test_round_trip(self) -> None:
        """Inversion recovers the σ used to compute the price."""
        sigma_true = 0.42
        s0 = Decimal("50000")
        st = Decimal("49800")
        tau = 5.0 / (365 * 24 * 60)

        price_dec = Decimal(str(round(fair_value_up(s0, st, tau, sigma_true), 6)))
        sigma_recovered = invert_sigma(price_dec, s0, st, tau)

        assert sigma_recovered is not None
        assert abs(sigma_recovered - sigma_true) < 1e-3

    def test_at_boundary_returns_none(self) -> None:
        """Prices at 0 or 1 return None (gotcha #11)."""
        assert invert_sigma(Decimal("0"), Decimal("50000"), Decimal("50000"), 0.01) is None
        assert invert_sigma(Decimal("1"), Decimal("50000"), Decimal("50000"), 0.01) is None

    def test_near_boundary_returns_none(self) -> None:
        assert invert_sigma(Decimal("1e-7"), Decimal("50000"), Decimal("50000"), 0.01) is None

    def test_zero_tte_returns_none(self) -> None:
        assert invert_sigma(Decimal("0.5"), Decimal("50000"), Decimal("50000"), 0.0) is None

    def test_batch_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            invert_sigma_batch(
                [Decimal("0.5")],
                Decimal("50000"),
                [Decimal("50000"), Decimal("51000")],
                [0.01],
            )

    def test_batch_round_trip(self) -> None:
        sigma_true = 0.55
        s0 = Decimal("3000")
        st_list = [Decimal("2970"), Decimal("3010"), Decimal("3020")]
        tau_list = [5.0 / (365 * 24 * 60)] * 3

        prices = [
            Decimal(str(round(fair_value_up(s0, st, tau, sigma_true), 6)))
            for st, tau in zip(st_list, tau_list, strict=False)
        ]
        results = invert_sigma_batch(prices, s0, st_list, tau_list)
        assert len(results) == 3
        for r in results:
            if r is not None:
                assert abs(r - sigma_true) < 1e-2
