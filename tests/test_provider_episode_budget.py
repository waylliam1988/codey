"""Episode timeouts stay within the configured budget, including float rounding."""

from unittest.mock import Mock, patch

import pytest

from codey.operations.project_audit_advisor import run_project_audit_advisor
from codey.operations.provider_session import DeadlineProvider
from codey.policies.task_policy import TaskPolicy


def test_real_advisor_send_never_rounds_above_total_budget(tmp_path):
    provider = Mock(spec=["send"])
    provider.send.return_value = '{"tool":"done","args":{"summary":"clean"}}'
    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    # A representable clock value whose deadline subtraction rounds upward.
    assert (76.013 + 180.0) - 76.013 > 180.0
    with patch("time.monotonic", return_value=76.013):
        assert run_project_audit_advisor(provider, tmp_path, "audit", parent_policy=policy) == "clean"
    assert provider.send.call_count == 1
    assert 0 < provider.send.call_args.kwargs["timeout"] <= 180.0


@pytest.mark.parametrize("method", ["send", "send_turn", "send_tool_results"])
def test_each_protocol_uses_elapsed_budget_and_shorter_request_timeout(method):
    provider = Mock(spec=[method])
    with patch("time.monotonic", return_value=1000.0):
        bounded = DeadlineProvider(provider, timeout=180.0)
    with patch("time.monotonic", return_value=1050.0):
        getattr(bounded, method)("message")
        assert getattr(provider, method).call_args.kwargs["timeout"] == 130.0
        getattr(bounded, method)("message", timeout=5.0)
        assert getattr(provider, method).call_args.kwargs["timeout"] == 5.0
    with patch("time.monotonic", return_value=1180.0), pytest.raises(TimeoutError):
        getattr(bounded, method)("message")
    assert getattr(provider, method).call_count == 2


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_invalid_episode_budget_never_constructs_a_live_adapter(timeout):
    with pytest.raises(ValueError, match="positive finite"):
        DeadlineProvider(Mock(), timeout=timeout)
