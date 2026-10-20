from pathlib import Path

from codey.durable import DurableSession, EffectRecord, SQLiteDurableStore
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectStore,
    prepare_intent,
)
from codey.runtime.effects.replay_policy import ReplayClass


def test_production_effect_projection_reads_durable_record_without_jsonl(tmp_path: Path) -> None:
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    durable.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="read file",
        provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="assistant_response_pending", state={})
    intent = prepare_intent(
        "conversation-1",
        "task-1",
        RuntimeEffectIntent(
            "effect-1", "tool_call", "conversation-1", "task-1",
            phase="writer", provider_id="local", tool_name="read", tool_id="0:0",
            call_id="call-1", args_digest="sha256:abc", replay_class=ReplayClass.SAFE,
        ),
    )
    durable.record_effect_intent(
        "task-1",
        EffectRecord(
            "effect-1", "task-1", "tool_call", "safe", "sha256:abc", "pending", intent.to_payload()
        ),
    )
    projections = RuntimeEffectStore(durable=durable).load_effects("conversation-1", "task-1")
    assert len(projections) == 1
    assert projections[0].intent.effect_id == "effect-1"
    assert projections[0].is_pending
