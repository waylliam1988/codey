"""Compact settled slots preserve status/text and reject missing status."""
import pytest

from codey.operations.kernel_recovery import replay_slot_typed
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.effect_records import compute_args_digest


@pytest.mark.parametrize("ok", [True, False])
def test_compact_slot_preserves_original_status_and_text(ok):
    call = ToolCall("read_file", {"path": "a.py"}, "c1")
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    text = "ERROR: literal source text" if ok else "source unavailable"
    session.executed["slot"] = {"name": call.name, "ok": ok, "excerpt": text,
                                "call_id": call.call_id, "args_digest": compute_args_digest(call.args)}
    slot = replay_slot_typed(session, "slot", call, call.name)
    assert slot.disposition == "RECOVERED"
    assert slot.result.ok is ok
    assert slot.result.model_text == text


@pytest.mark.parametrize("status", ["missing", None, 0, 1, "false"])
def test_compact_slot_requires_exact_boolean_status(status):
    call = ToolCall("read_file", {"path": "a.py"}, "c1")
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    row = {"name": call.name, "excerpt": "body", "args_digest": compute_args_digest(call.args)}
    if status != "missing":
        row["ok"] = status
    session.executed["slot"] = row
    slot = replay_slot_typed(session, "slot", call, call.name)
    assert slot.disposition == "FAILED"
    assert slot.result.ok is False
