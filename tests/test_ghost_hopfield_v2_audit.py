from __future__ import annotations

import math

import pytest

from tests.manual.ghost_memory_routing.hopfield_v2_audit import positive_mass_upper, zero_difference_bound


@pytest.mark.parametrize("n", (12, 96))
def test_exact_bound_reproduces_zero_event_probability(n: int) -> None:
    result = zero_difference_bound(n)
    upper = result["upper"]
    assert isinstance(upper, float)
    assert (1 - upper) ** n == pytest.approx(0.0125)
    assert result["lower"] == -upper
    assert upper > 0


def test_small_live_sample_does_not_inherit_heldout_precision() -> None:
    assert zero_difference_bound(96)["excludes_five_point_gain"] is True
    assert zero_difference_bound(12)["excludes_five_point_gain"] is False


@pytest.mark.parametrize("n,alpha", [(0, 0.05), (12, 0.0), (12, 1.0)])
def test_invalid_bound_inputs(n: int, alpha: float) -> None:
    with pytest.raises(ValueError):
        zero_difference_bound(n, alpha)


def test_live_no_observed_win_does_not_prove_no_unseen_gain() -> None:
    assert positive_mass_upper(12, 0) > 0.30
    assert positive_mass_upper(96, 0) < 0.05


def test_exact_positive_mass_bound_inverts_binomial_probability() -> None:
    n, positives = 96, 3
    probability = positive_mass_upper(n, positives)
    cdf = sum(math.comb(n, count) * probability ** count * (1 - probability) ** (n - count)
              for count in range(positives + 1))
    assert cdf == pytest.approx(0.0125)


def test_all_positive_clusters_have_trivial_upper_bound() -> None:
    assert positive_mass_upper(12, 12) == 1.0
