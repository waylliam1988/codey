from pathlib import Path
from types import SimpleNamespace

from codey.durable import DurableSession, EffectRecord, MemoryDurableStore
from codey.durable.model import thaw_json
from codey.runtime.core.operation_state import lane_for_run, operation_id_for_run
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    compute_args_digest,
    prepare_intent,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.write.mutation_line import RuntimeMutationLine
from tests.support.receipt_fixtures import inspect_receipt_facts


def test_missing_operation_preserves_unconfirmed_provider_facts() -> None:
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="continue",
        provider_snapshot={"provider_id": "local", "model_id": "model-1"},
    )
    durable.start_task("task-1", state={})
    durable.record_effect_intent(
        "task-1",
        EffectRecord(
            effect_id="effect-1",
            task_id="task-1",
            category="provider_send",
            replay_class="unsafe",
            args_digest="digest-1",
            status="pending",
            intent={
                "lane": "provider",
                "operation_id": "operation-1",
                "session_id": "conversation-1",
                "run_id": "task-1",
            },
        ),
    )

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1",
        run_id="task-1",
        project="",
        task_kind="chat",
    )

    assert result.ok is False
    assert durable.store.effect("effect-1").status == "pending"
    assert durable.task("task-1").phase == "running"


def test_missing_operation_does_not_hide_an_already_unknown_provider_outcome() -> None:
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="continue",
        provider_snapshot={"provider_id": "local", "model_id": "model-1"},
    )
    durable.start_task("task-1", state={})
    durable.record_effect_intent(
        "task-1",
        EffectRecord(
            effect_id="effect-1",
            task_id="task-1",
            category="provider_send",
            replay_class="unsafe",
            args_digest="digest-1",
            status="pending",
            intent={
                "lane": "provider",
                "operation_id": "operation-1",
                "session_id": "conversation-1",
                "run_id": "task-1",
            },
        ),
    )
    durable.settle_effect(
        "task-1",
        EffectRecord(
            effect_id="effect-1",
            task_id="task-1",
            category="provider_send",
            replay_class="unsafe",
            args_digest="digest-1",
            status="error",
            intent={
                "lane": "provider",
                "operation_id": "operation-1",
                "session_id": "conversation-1",
                "run_id": "task-1",
            },
            settlement={"sent_state": "maybe_sent", "status": "error"},
        ),
    )

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1",
        run_id="task-1",
        project="",
        task_kind="chat",
    )

    assert result.ok is False
    assert durable.task("task-1").phase == "running"
    assert durable.store.effect("effect-1").settlement["sent_state"] == "maybe_sent"


def test_missing_operation_does_not_synthesize_an_unsafe_tool_outcome(tmp_path: Path) -> None:
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="edit a file", provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.record_effect_intent(
        "task-1",
        EffectRecord(
            "effect-1", "task-1", "tool_call", "unsafe", "digest-1", "pending",
            RuntimeEffectIntent(
                "effect-1", EFFECT_CATEGORY_TOOL_CALL, "conversation-1", "task-1",
                lane=lane_for_run("task-1"), operation_id=operation_id_for_run("task-1"), tool_name="edit",
                args_digest="digest-1", replay_class=ReplayClass.UNSAFE,
            ).to_payload(),
        ),
    )

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1", run_id="task-1",
        project=str(tmp_path), task_kind="project",
    )

    assert result.ok is False
    assert durable.store.effect("effect-1").status == "pending"
    assert durable.task("task-1").phase == "running"


def test_task_policy_is_recovered_from_checkpoint_and_cannot_be_replaced() -> None:
    from codey.durable.errors import CommitConflict

    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="write", provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    policy = {"grants": ["project.write"], "required_checks": ["completion"]}

    durable.record_task_policy("task-1", policy)
    assert thaw_json(durable.task("task-1").state["task_policy"]) == policy
    durable.record_task_policy("task-1", policy)
    try:
        durable.record_task_policy("task-1", {"grants": []})
    except CommitConflict:
        pass
    else:
        raise AssertionError("task authorization snapshot was replaced")


def test_production_recovery_requires_an_admitted_durable_task(tmp_path: Path) -> None:
    durable = DurableSession(MemoryDurableStore())
    mutations = RuntimeMutationLine(durable=durable)
    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=mutations,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1", run_id="task-1",
        project=str(tmp_path), task_kind="project",
    )

    assert result.ok is False


def test_recovery_never_uses_runtime_projection_as_authority(tmp_path: Path) -> None:
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="continue", provider_snapshot={"provider_id": "local"},
    )
    durable.claim_task("task-1")
    durable.start_task("task-1", state={})
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(
        session_id="conversation-1", run_id="task-1", project=str(tmp_path),
        provider_id="local", turn_budget=3, max_repair_rounds=0, task_kind="project",
    )
    mutations.mark_writer_running("conversation-1", "task-1", provider_id="local")

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=mutations,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1", run_id="task-1", project=str(tmp_path), task_kind="project",
    )

    assert result.ok is True


def test_pending_durable_tool_intent_cannot_be_overruled_by_idle_runtime_log(tmp_path: Path) -> None:
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="read a file", provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.record_effect_intent(
        "task-1",
        EffectRecord(
            "effect-1", "task-1", "tool_call", "safe", "digest-1", "pending",
            RuntimeEffectIntent(
                "effect-1", EFFECT_CATEGORY_TOOL_CALL, "conversation-1", "task-1",
                lane=lane_for_run("task-1"), operation_id=operation_id_for_run("task-1"),
                tool_name="read", args_digest="digest-1", replay_class=ReplayClass.SAFE,
                replay_args={"path": "a.txt"},
            ).to_payload(),
        ),
    )
    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1", run_id="task-1", project="", task_kind="chat",
    )

    assert result.ok is False


def test_safe_tool_requires_operation_and_delivery_facts_before_replay(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("durable read", encoding="utf-8")
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="read a file", provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    raw_intent = RuntimeEffectIntent(
        effect_id="effect-1", effect_category=EFFECT_CATEGORY_TOOL_CALL,
        session_id="conversation-1", run_id="task-1", phase="writer",
        turn=1, tool_index=0, tool_name="read", replay_class=ReplayClass.SAFE,
        args_digest=compute_args_digest({"path": "a.txt"}),
        replay_args={"path": "a.txt"},
    )
    intent = prepare_intent("conversation-1", "task-1", raw_intent)
    durable.record_effect_intent(
        "task-1",
        EffectRecord(
            "effect-1", "task-1", "tool_call", "safe", intent.args_digest,
            "pending", intent.to_payload(),
        ),
    )

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
            managed_outputs=None,
        ),
        session_id="conversation-1", run_id="task-1", project=str(project), task_kind="project",
    )

    assert result.ok is False
    assert result.recovered_tool_outcomes == ()
    assert (project / "a.txt").read_text(encoding="utf-8") == "durable read"
    assert durable.store.effect("effect-1").status == "pending"


def test_safe_tool_recovery_blocks_an_intent_unbound_to_the_operation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("projection repair", encoding="utf-8")
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="task-1", task_id="task-1",
        text="read a file", provider_snapshot={"provider_id": "local"},
    )
    durable.claim_task("task-1")
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    intent = prepare_intent(
        "conversation-1", "task-1",
        RuntimeEffectIntent(
            "effect-1", EFFECT_CATEGORY_TOOL_CALL, "conversation-1", "task-1",
            phase="writer", turn=1, tool_index=0, tool_name="read",
            replay_class=ReplayClass.SAFE,
            args_digest=compute_args_digest({"path": "a.txt"}),
            replay_args={"path": "a.txt"},
        ),
    )
    durable.record_effect_intent(
        "task-1", EffectRecord(
            "effect-1", "task-1", "tool_call", "safe", intent.args_digest,
            "pending", intent.to_payload(),
        ),
    )
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(
        session_id="conversation-1", run_id="task-1", project=str(project),
        provider_id="local", turn_budget=4, max_repair_rounds=0, task_kind="project",
    )
    mutations.mark_writer_running("conversation-1", "task-1", provider_id="local")

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
            managed_outputs=None,
        ),
        session_id="conversation-1", run_id="task-1", project=str(project), task_kind="project",
    )

    assert result.ok is False
    assert result.recovered_tool_result_batch_id == ""
    assert result.recovered_tool_outcomes == ()
    assert durable.store.effect("effect-1").status == "pending"


def test_safe_tool_recovery_reuses_existing_runtime_intent_and_batch(tmp_path: Path) -> None:
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import (
        DeliveryBatchIntent,
        DeliveryBatchItem,
        ToolResultDeliveryStore,
        compute_batch_digest,
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("already projected", encoding="utf-8")
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="task-1", task_id="task-1",
        text="read a file", provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    intent = prepare_intent(
        "conversation-1", "task-1",
        RuntimeEffectIntent(
            "effect-1", EFFECT_CATEGORY_TOOL_CALL, "conversation-1", "task-1",
            phase="writer", turn=1, tool_index=0, tool_name="read",
            replay_class=ReplayClass.SAFE,
            args_digest=compute_args_digest({"path": "a.txt"}),
            replay_args={"path": "a.txt"},
        ),
    )
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(
        session_id="conversation-1", run_id="task-1", project=str(project),
        provider_id="local", turn_budget=4, max_repair_rounds=0, task_kind="project",
    )
    mutations.mark_writer_running("conversation-1", "task-1", provider_id="local")
    items = (DeliveryBatchItem(0, "read", "effect-1", "safe", False),)
    mutations.begin_tool_batch(
        "conversation-1", "task-1", intents=(intent,),
        delivery_intent=DeliveryBatchIntent(
            batch_id="batch-existing", session_id="conversation-1", run_id="task-1",
            turn=1, items=items, batch_digest=compute_batch_digest(items),
        ),
    )
    deps = SimpleNamespace(
        state=SimpleNamespace(durable=durable),
        runtime_mutations=mutations,
        runtime_effects=RuntimeEffectStore(durable=durable),
        tool_result_delivery=ToolResultDeliveryStore(durable=durable),
        managed_outputs=None,
    )

    result = inspect_receipt_facts(
        deps, session_id="conversation-1", run_id="task-1",
        project=str(project), task_kind="project",
    )

    assert result.ok is True
    assert result.recovered_tool_result_batch_id == "batch-existing"
    assert result.recovered_tool_outcomes == ()
    projections = RuntimeEffectStore(durable=durable).load_effects("conversation-1", "task-1")
    assert [item.intent.effect_id for item in projections] == ["effect-1"]
    assert projections[0].settlement is None
    from codey.runtime.write.drive import peek_next_action
    action = peek_next_action(durable, session_id="conversation-1", run_id="task-1")
    assert action.kind == "replay_safe_tool_batch" and action.delivery_batch_id == "batch-existing"


def test_mixed_unknown_effects_preserve_the_unsettled_member_without_replay(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("safe read", encoding="utf-8")
    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="conversation-1", request_id="task-1", task_id="task-1",
        text="mixed batch", provider_snapshot={"provider_id": "local"},
    )
    durable.start_task("task-1", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    durable.advance_task(task_id="task-1", phase="running", state={})
    safe_intent = prepare_intent(
        "conversation-1", "task-1",
        RuntimeEffectIntent(
            "effect-safe", EFFECT_CATEGORY_TOOL_CALL, "conversation-1", "task-1",
            phase="writer", turn=1, tool_index=1, tool_name="read",
            replay_class=ReplayClass.SAFE,
            args_digest=compute_args_digest({"path": "a.txt"}),
            replay_args={"path": "a.txt"},
            delivery_batch_id="batch-mixed",
        ),
    )
    unsafe_intent = prepare_intent(
        "conversation-1", "task-1",
        RuntimeEffectIntent(
            "effect-unsafe", EFFECT_CATEGORY_TOOL_CALL, "conversation-1", "task-1",
            phase="writer", turn=1, tool_index=0, tool_name="edit",
            replay_class=ReplayClass.UNSAFE,
            args_digest=compute_args_digest({"path": "a.txt", "content": "changed"}),
            delivery_batch_id="batch-mixed",
        ),
    )
    durable.record_effect_intents(
        "task-1",
        tuple(
            EffectRecord(
                intent.effect_id, "task-1", EFFECT_CATEGORY_TOOL_CALL,
                intent.replay_class, intent.args_digest, "pending", intent.to_payload(),
            )
            for intent in (unsafe_intent, safe_intent)
        ),
    )
    durable.settle_unknown_effect("task-1", "effect-unsafe")

    result = inspect_receipt_facts(
        SimpleNamespace(
            state=SimpleNamespace(durable=durable),
            runtime_mutations=None,
            runtime_effects=None,
            tool_result_delivery=None,
        ),
        session_id="conversation-1", run_id="task-1",
        project=str(project), task_kind="project",
    )

    assert result.ok is False
    assert durable.store.effect("effect-safe").status == "pending"
    assert durable.task("task-1").phase == "running"
