from codey.durable import EffectRecord, TaskCheckpoint, decide_recovery


def _task(phase: str) -> TaskCheckpoint:
    return TaskCheckpoint(
        "task-1", "conversation-1", "submission-1", phase, 2,
        {"provider_id": "local"}, {},
    )


def _effect(category: str, *, settled: bool = False, replay_class: str = "unsafe") -> EffectRecord:
    return EffectRecord(
        f"{category}-1", "task-1", category, replay_class, "digest-1",
        "settled" if settled else "pending", {},
        {"status": "ok"} if settled else None,
    )


def test_recovery_marks_uncertain_provider_call_unknown_without_retry() -> None:
    decision = decide_recovery(_task("assistant_intent_committed"), (_effect("provider_send"),))
    assert decision.action == "settle_provider_unknown"
    assert decision.effect_ids == ("provider_send-1",)


def test_recovery_routes_pending_safe_tools_to_durable_recovery() -> None:
    decision = decide_recovery(_task("tool_effect_running"), (_effect("tool_call", replay_class="safe"),))
    assert decision.action == "recover_tool_effects"


def test_recovery_settles_pending_unsafe_tools_without_replay() -> None:
    decision = decide_recovery(_task("tool_effect_running"), (_effect("tool_call"),))
    assert decision.action == "settle_tool_unknown"


def test_recovery_rejects_effects_that_do_not_match_checkpoint() -> None:
    decision = decide_recovery(_task("assistant_response_pending"), (_effect("tool_call"),))
    assert decision.action == "fail_invariant"


def test_recovery_reconciles_deferred_provider_after_tool_receipt() -> None:
    task = _task("tool_effect_running")
    provider = _effect("provider_send")
    tool = _effect("tool_call", settled=True)
    decision = decide_recovery(task, (provider, tool))
    assert decision.action == "reconcile_provider_generation"
    assert decision.effect_ids == ("provider_send-1",)
