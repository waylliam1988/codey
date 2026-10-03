"""Malformed context budgets retain the documented default, not the minimum."""

import pytest

from codey.research.evidence_rules import _context_char_limit


@pytest.mark.parametrize("value", [None, "invalid", float("inf"), float("nan")])
def test_malformed_budget_uses_default(value):
    assert _context_char_limit(value) == 8000


def test_valid_small_budget_is_still_bounded():
    assert _context_char_limit(0) == 2000
