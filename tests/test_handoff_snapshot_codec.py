"""Persisted handoff fields retain their types and legacy tuple support."""

import pytest

from codey.agents.handoff import ConversationSnapshot
from codey.types.json_value import parse_json_object


@pytest.mark.parametrize("files", [["a.py", "b.py"], ("a.py", "b.py")], ids=["list", "tuple"])
@pytest.mark.parametrize("checks,passed", [(None, None), ("passed", True), ("not passed", False)])
def test_snapshot_restores_typed_fields_and_ignores_unrelated_metadata(
    files: object, checks: str | None, passed: bool | None,
) -> None:
    payload: dict[str, object] = {
        "mode": "project", "goal": "continue", "previous_model": "provider", "changed_files": files,
        "checks": checks, "conversation_summary": {"current_state": "ready"}, "future_metadata": object(),
    }

    snapshot = ConversationSnapshot.from_payload(payload)

    assert snapshot.mode == "project"
    assert snapshot.goal == "continue"
    assert snapshot.provider_id == "provider"
    assert snapshot.changed_files == ("a.py", "b.py")
    assert snapshot.checks_passed is passed
    assert parse_json_object(snapshot.conversation_summary) == {"current_state": "ready"}
    assert "future_metadata" not in snapshot.to_payload()


@pytest.mark.parametrize("summary", [["ready"], ("ready",)], ids=["list", "tuple"])
def test_snapshot_restores_sequence_summary_as_json_data(summary: object) -> None:
    snapshot = ConversationSnapshot.from_payload({"conversation_summary": summary})

    assert snapshot.mode == "chat"
    assert snapshot.to_payload()["conversation_summary"] == ["ready"]


@pytest.mark.parametrize("field,value", [
    ("mode", ""), ("goal", 1), ("previous_model", None), ("changed_files", "a.py"),
    ("changed_files", ["a.py", 1]), ("checks", True), ("conversation_summary", 1),
])
def test_snapshot_rejects_invalid_known_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ConversationSnapshot.from_payload({field: value})
