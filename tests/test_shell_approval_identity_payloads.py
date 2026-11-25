"""Request and persisted approval projections preserve the same native call identity."""

import pytest

from codey.agents.shell_approval import ShellApprovalRequest, build_shell_approval_pending
from codey.types.protocol_window import ProtocolWindowRef, ProviderBinding
from codey.utils.object_fields import object_fields

_IDENTITY_KEYS = frozenset({"call_id", "call_id_invalid", "turn", "tool_index", "window_ref"})


def _pending(approval: ShellApprovalRequest) -> dict[str, object]:
    return build_shell_approval_pending(
        approval=approval, approval_id="approval", session_id="session", run_id="run",
        project=".", attached_project=None, intent="project", max_turns=4, provider_label="fixture",
    )


def _identity(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key in _IDENTITY_KEYS}


@pytest.mark.parametrize("call_id,expected_call", [
    ("", {}), ("   ", {}),
    (" native identity ", {"call_id": " native identity "}),
    ("x" * 256, {"call_id": "x" * 256}),
    ("x" * 257, {"call_id_invalid": True}),
])
def test_request_and_pending_keep_exact_call_identity_and_window(
    call_id: str, expected_call: dict[str, object],
) -> None:
    binding = ProviderBinding("connection", "revision", "model", "native_openai", "history")
    window = ProtocolWindowRef("session", binding, "window")
    approval = ShellApprovalRequest(".", "echo hi", call_id=call_id, turn=3, tool_index=2, window_ref=window)
    expected = {**expected_call, "turn": 3, "tool_index": 2, "window_ref": window.to_payload()}

    assert _identity(approval.to_payload()) == expected
    pending = _pending(approval)
    assert _identity(pending) == expected
    event = pending["ui_event"]
    assert object_fields(event)
    if "call_id" in expected_call:
        assert event["call_id"] == expected_call["call_id"]
    else:
        assert "call_id" not in event
    assert "call_id_invalid" not in event


@pytest.mark.parametrize("turn,tool_index,expected", [
    (-1, 3, {"tool_index": 3}), (3, -1, {"turn": 3}), (True, False, {}),
])
def test_invalid_numeric_identity_fields_are_omitted_in_each_projection(
    turn: int, tool_index: int, expected: dict[str, object],
) -> None:
    approval = ShellApprovalRequest(".", "echo hi", turn=turn, tool_index=tool_index)

    assert _identity(approval.to_payload()) == expected
    assert _identity(_pending(approval)) == expected


def test_each_projection_keeps_its_existing_provider_id_bound() -> None:
    approval = ShellApprovalRequest(".", "echo hi", provider_id=" " + "p" * 241 + " ")

    assert approval.to_payload()["provider_id"] == "p" * 240
    assert _pending(approval)["provider_id"] == "p" * 241
