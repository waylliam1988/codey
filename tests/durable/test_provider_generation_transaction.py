from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore, SubmissionPlacement
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectSettlement
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.write.mutation_line import RuntimeMutationLine


def _runtime(tmp_path: Path) -> tuple[DurableSession, RuntimeMutationLine, str]:
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    steer_result = durable.admit_input(
        conversation_id="s", request_id="request", task_id="r", text="read",
        provider_snapshot={"provider_id": "local"},
    )
    durable.claim_task("r")
    durable.start_task("r", state=dict(durable.task("r").state))
    durable.advance_task(task_id="r", phase="running", state={})
    durable.admit_input(
        conversation_id="s", request_id="steer", task_id="steer", text="change",
        provider_snapshot={"provider_id": "local"}, placement=SubmissionPlacement.QUEUED,
        mode="steer",
    )
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=8,
                          max_repair_rounds=1, task_kind="project")
    line.mark_writer_running("s", "r", provider_id="local")
    line.begin_provider_effect("s", "r", RuntimeEffectIntent(
        "p1", "provider_send", "s", "r", provider_id="local", turn=1,
        args_digest="a" * 64, replay_class=ReplayClass.UNSAFE,
    ))
    return durable, line, str(steer_result.submission_id)


def _settlement() -> RuntimeEffectSettlement:
    return RuntimeEffectSettlement("p1", "provider_send", "s", "r", "ok", sent_state="settled")


def test_provider_settlement_boundary_and_placement_are_one_commit(tmp_path: Path) -> None:
    durable, line, _steer_submission_id = _runtime(tmp_path)
    before = durable.commit_sequence()

    placed = line.settle_provider_effect_at_boundary("s", "r", _settlement(), boundary="post_tools")

    assert durable.commit_sequence() == before + 1
    assert durable.effect("p1").settlement is not None
    assert durable.task("r").state["generation_boundary"] == "post_tools"
    assert [item.request_id for item in placed] == ["steer"]
    assert durable.store.submission(str(placed[0].submission_id)).placement is SubmissionPlacement.PLACED
    durable.close()


def test_provider_generation_commit_failure_rolls_back_all_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    durable, line, steer_submission_id = _runtime(tmp_path)
    before_task = durable.task("r")
    before_submission = durable.store.submission(steer_submission_id)
    before_effect = durable.effect("p1")

    def fail(_mutation: object) -> object:
        raise OSError("injected generation commit failure")

    monkeypatch.setattr(durable.store, "commit", fail)
    with pytest.raises(OSError, match="generation commit"):
        line.settle_provider_effect_at_boundary("s", "r", _settlement(), boundary="post_tools")

    durable.close()
    reopened = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        assert reopened.task("r") == before_task
        assert reopened.store.submission(steer_submission_id) == before_submission
        assert reopened.effect("p1") == before_effect
    finally:
        reopened.close()
