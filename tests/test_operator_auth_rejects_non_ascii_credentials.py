"""Malformed credential strings must be refused without crashing authentication."""
import pytest

from codey.app.operator_auth import OperatorAuth


@pytest.mark.parametrize("token", ["\ud800", "汉字", "🙂", "", 1, None])
def test_invalid_bootstrap_does_not_raise_or_consume_valid_nonce(token):
    auth = OperatorAuth(1234)
    valid = auth.issue_bootstrap()
    assert auth.exchange(token) is None
    assert auth.exchange(valid) is not None
