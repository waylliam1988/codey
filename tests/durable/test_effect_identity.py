"""Logical call identity is independent from an operation's argument digest."""

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore


@pytest.fixture(params=["memory", "sqlite"])
def session(request: pytest.FixtureRequest, tmp_path: Path) -> DurableSession:
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.admit_input(conversation_id="c", request_id="r", task_id="t", text="read", provider_snapshot={})
    session.start_task("t", state={})
    session.advance_task(task_id="t", phase="assistant_intent_committed", state={})
    session.advance_task(task_id="t", phase="assistant_response_pending", state={})
    return session


def intent(effect_id: str = "e") -> EffectRecord:
    return EffectRecord(effect_id, "t", "tool_call", "safe", "same-args-digest", "pending", {
        "tool": "read", "args": {"path": "file", "limit": 10},
    })


def test_distinct_logical_calls_can_use_identical_arguments(session: DurableSession) -> None:
    session.record_effect_intents("t", (intent("a"), intent("b")))
    assert {effect.effect_id for effect in session.effects("t")} == {"a", "b"}


def test_repeated_intent_cannot_authorize_another_execution(session: DurableSession) -> None:
    session.record_effect_intent("t", intent())
    before = session.commit_sequence()
    with pytest.raises(CommitConflict):
        session.record_effect_intent("t", intent())
    assert session.commit_sequence() == before


def test_identical_settlement_retry_does_not_advance_checkpoint_or_publish(session: DurableSession) -> None:
    session.record_effect_intent("t", intent())
    settled = replace(intent(), status="ok", settlement={"text": "receipt"})
    session.settle_effect("t", settled)
    before = session.commit_sequence()
    task = session.task("t")
    result = session.settle_effect("t", settled)
    assert result.record is None
    assert session.commit_sequence() == before
    assert session.task("t") == task


def test_settlement_identity_does_not_depend_on_mapping_key_order(session: DurableSession) -> None:
    session.record_effect_intent("t", intent())
    reordered = replace(intent(), intent={"args": {"limit": 10, "path": "file"}, "tool": "read"},
                        status="ok", settlement={"text": "receipt"})
    session.settle_effect("t", reordered)
    assert session.effect("e").status == "ok"


def test_settlement_requires_an_outcome_and_cannot_erase_the_pending_effect(session: DurableSession) -> None:
    session.record_effect_intent("t", intent())
    before = session.commit_sequence()
    with pytest.raises(ValueError, match="outcome"):
        session.settle_effect("t", intent())
    assert session.commit_sequence() == before
    assert session.task("t").pending_effect_id == "e"


@pytest.mark.parametrize("sent_state,phase", [("maybe_sent", "assistant_response_unknown"), ("not_sent", "provider_prepared")])
def test_provider_transport_uncertainty_determines_the_next_allowed_phase(session: DurableSession, sent_state: str, phase: str) -> None:
    effect = EffectRecord("provider", "t", "provider_send", "unsafe", "digest", "pending", {})
    session.record_effect_intent("t", effect)
    session.settle_effect("t", replace(effect, status="error", settlement={"sent_state": sent_state}))
    assert session.task("t").phase == phase
    if sent_state == "maybe_sent":
        before = session.commit_sequence()
        with pytest.raises(CommitConflict, match="phase"):
            session.record_effect_intent("t", replace(effect, effect_id="retry"))
        assert session.commit_sequence() == before
    else:
        session.record_effect_intent("t", replace(effect, effect_id="retry"))


def test_terminal_settlement_cannot_be_overwritten_even_with_its_current_status(session: DurableSession) -> None:
    session.record_effect_intent("t", intent())
    settled = replace(intent(), status="ok", settlement={"text": "original"})
    session.settle_effect("t", settled)
    with pytest.raises(CommitConflict):
        session.settle_effect("t", replace(settled, status="error", settlement={"text": "changed"}), expected_status="ok")
    assert session.effect("e") == settled
