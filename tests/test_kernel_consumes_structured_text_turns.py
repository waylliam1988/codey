"""The kernel consumes decoded text turns without changing authorization."""

from __future__ import annotations

from types import SimpleNamespace

from codey.operations.kernel_protocol import normalize_turn
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn

POLICY = TaskPolicy(grants=frozenset({"project.read", "control"}))


def test_structured_text_request_uses_the_shared_kernel_validation() -> None:
    decoded = decode_text_turn('{"tool":"read_file","args":{"path":"inspect.py"}}')

    plan = normalize_turn(decoded, policy=POLICY)

    assert not plan.protocol_error
    assert len(plan.calls) == 1
    assert plan.calls[0].name == "read_file"
    assert plan.calls[0].args["path"] == "inspect.py"


def test_structured_text_error_keeps_protocol_diagnostics() -> None:
    decoded = decode_text_turn('{"tool":"read_file","args":{"path":"inspect.py\n"}}')

    plan = normalize_turn(decoded, policy=SimpleNamespace(allows=lambda _grant: True))

    assert not plan.calls
    assert plan.protocol_error_kind == "invalid_json"
    assert "escape" in plan.protocol_error.lower()


def test_kernel_rejects_raw_text_without_running_the_text_decoder() -> None:
    plan = normalize_turn('{"tool":"read_file","args":{"path":"inspect.py"}}', policy=POLICY)

    assert plan.calls == []
    assert plan.protocol_error_kind == "raw_text"
    assert "decoded" in plan.protocol_error
