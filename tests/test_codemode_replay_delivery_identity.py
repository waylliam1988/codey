import pytest

from codey.codemode.replay import CodeModeReplayError, DurableCodeModeReplay
from codey.durable import DurableSession, MemoryDurableStore
from codey.types.json_value import canonical_json_object


def test_delivery_refuses_a_checkpoint_with_changed_replay_identity() -> None:
    durable = DurableSession(MemoryDurableStore())
    try:
        durable.admit_input(
            conversation_id="chat", request_id="request", task_id="task", text="run",
            provider_snapshot={"provider_id": "test"},
        )
        replay = DurableCodeModeReplay(
            durable, "task", "execution", "original-fingerprint",
            parent_effect_id="parent", runtime_contract="original-runtime",
        )
        replay.record_delivery("first", 0)
        assert replay.delivery == ({"call_id": "first", "child_index": 0},)

        journal = canonical_json_object(durable.task("task").state["codemode_replay"])
        record = journal["execution"]
        assert isinstance(record, dict)
        record["fingerprint"] = "changed-fingerprint"
        durable.update_task_state("task", {"codemode_replay": journal})

        before_delivery = durable.task("task")
        with pytest.raises(CodeModeReplayError, match="identity or metadata mismatch"):
            replay.record_delivery("second", 1)
        assert durable.task("task") == before_delivery
    finally:
        durable.close()


def test_delivery_refuses_admission_after_an_error_without_a_message() -> None:
    durable = DurableSession(MemoryDurableStore())
    try:
        durable.admit_input(
            conversation_id="chat", request_id="request", task_id="task", text="run",
            provider_snapshot={"provider_id": "test"},
        )
        replay = DurableCodeModeReplay(
            durable, "task", "execution", "fingerprint",
            parent_effect_id="parent", runtime_contract="runtime",
        )
        with pytest.raises(ValueError), replay.admission():
            raise ValueError()

        checkpoint = durable.task("task")
        with pytest.raises(CodeModeReplayError):
            replay.record_delivery("too-late", 0)
        assert durable.task("task") == checkpoint
        with pytest.raises(CodeModeReplayError):
            replay.assert_valid()
    finally:
        durable.close()
