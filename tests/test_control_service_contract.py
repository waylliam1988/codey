"""Control commands use the real durable owner without publishing a runtime copy."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codey.app.context import AppContext
from codey.app.control_service import ControlService
from codey.control.protocol import Actor, ControlError
from codey.types.json_value import JsonValue
from codey.utils.object_fields import object_fields, sequence_items

if TYPE_CHECKING:
    from codey.launcher.releases import _ActivationOwner

AGENT = Actor("codex", "Codex")
USER = Actor("user", "User", True)


@dataclass
class Harness:
    ctx: AppContext
    service: ControlService
    project: Path


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[Harness]:
    project = tmp_path / "project"
    project.mkdir()
    ctx = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    service: ControlService | None = None
    try:
        service = ControlService(ctx, allowed_roots=(project,))
        yield Harness(ctx, service, project)
    finally:
        if service is not None:
            service.close()
        ctx.close()


def text(fields: dict[str, object], key: str) -> str:
    value = fields[key]
    assert isinstance(value, str)
    return value


def integer(fields: dict[str, object], key: str) -> int:
    value = fields[key]
    assert type(value) is int
    return value


def create(harness: Harness) -> dict[str, object]:
    return harness.service.execute(AGENT, "create_conversation", {
        "request_id": "create", "project": str(harness.project), "provider": "deepseek",
    })


def send(harness: Harness, config: dict[str, object], *, content: str = "Inspect read only.") -> dict[str, object]:
    return harness.service.execute(AGENT, "send_message", {
        "conversation_id": text(config, "conversation_id"), "control_generation": integer(config, "control_generation"),
        "request_id": "send", "text": content,
    })


def test_creation_receipt_inventory_and_admission_are_idempotent(harness: Harness) -> None:
    config = create(harness)
    assert create(harness) == config
    session = text(config, "conversation_id")
    state = harness.ctx.load_ui_state()
    sessions = state["sessions"]
    assert sequence_items(sessions)
    ids: list[str] = []
    for row in sessions:
        assert object_fields(row)
        ids.append(text(row, "id"))
    assert session in ids
    admission = send(harness, config)
    assert send(harness, config) == admission
    assert len(harness.ctx.durable.tasks()) == 1
    read = harness.service.execute(AGENT, "read_conversation", {"conversation_id": session})
    history = read["history"]
    assert object_fields(history)
    items = history["items"]
    assert sequence_items(items)
    kinds: list[str] = []
    for item in items:
        assert object_fields(item)
        kinds.append(text(item, "type"))
    assert "user" in kinds
    assert not {"control_config", "control_receipt"}.intersection(kinds)


def test_reused_input_id_cannot_change_its_content(harness: Harness) -> None:
    config = create(harness)
    admission = send(harness, config)
    with pytest.raises(ControlError) as failure:
        send(harness, config, content="Different input")
    assert failure.value.code == "REQUEST_CONFLICT"
    assert len(harness.ctx.durable.tasks()) == 1
    assert send(harness, config) == admission


def test_observer_timeout_does_not_cancel_a_queued_run(harness: Harness) -> None:
    config = create(harness)
    admission = send(harness, config)
    run_id = text(admission, "run_id")
    session = text(config, "conversation_id")
    result = harness.service.execute(AGENT, "wait_run", {
        "conversation_id": session, "run_id": run_id, "timeout_seconds": 0,
    })
    assert result["reason"] == "timeout"
    snapshot = result["snapshot"]
    assert object_fields(snapshot)
    assert snapshot["terminal"] is False
    assert harness.ctx.durable.task(run_id).state.get("cancel_requested") is not True
    assert harness.service.change_sequence > 0


def test_pausing_revokes_a_generation_and_preserves_its_receipt(harness: Harness) -> None:
    config = create(harness)
    admission = send(harness, config)
    session = text(config, "conversation_id")
    params: dict[str, object] = {
        "conversation_id": session, "control_generation": integer(config, "control_generation"), "request_id": "pause",
    }
    paused = harness.service.execute(AGENT, "pause_control", params)
    assert paused["mode"] == "paused"
    assert integer(paused, "control_generation") == integer(config, "control_generation") + 1
    assert harness.ctx.durable.task(text(admission, "run_id")).state["cancel_requested"] is True
    assert harness.service.execute(AGENT, "pause_control", params) == paused
    with pytest.raises(ControlError) as failure:
        harness.service.execute(AGENT, "send_message", {
            "conversation_id": session, "control_generation": integer(config, "control_generation"),
            "request_id": "stale", "text": "Inspect",
        })
    assert failure.value.code == "STALE_CONTROL"


def test_operator_and_conversation_identity_fences_are_independent(harness: Harness) -> None:
    config = create(harness)
    session = text(config, "conversation_id")
    other = Actor("other", "Other")
    assert harness.service.execute(other, "list_conversations", {})["conversations"] == []
    with pytest.raises(ControlError) as failure:
        harness.service.execute(other, "get_control", {"conversation_id": session})
    assert failure.value.code == "FORBIDDEN"
    with pytest.raises(ControlError) as failure:
        harness.service.execute(AGENT, "grant_control", {
            "conversation_id": session, "control_generation": integer(config, "control_generation"), "request_id": "grant",
        })
    assert failure.value.code == "FORBIDDEN"
    assert harness.service.execute(USER, "get_control", {"conversation_id": session}) == config


def test_review_requires_a_settled_candidate(harness: Harness) -> None:
    config = create(harness)
    admission = send(harness, config)
    session = text(config, "conversation_id")
    run_id = text(admission, "run_id")
    candidate = harness.service.execute(AGENT, "get_run_result", {"conversation_id": session, "run_id": run_id})
    before = harness.ctx.durable.entries(session)
    with pytest.raises(ControlError) as failure:
        harness.service.execute(AGENT, "record_review", {
            "conversation_id": session, "run_id": run_id, "control_generation": integer(config, "control_generation"),
            "request_id": "review", "candidate_digest": text(candidate, "candidate_digest"),
            "passed": True, "summary": "Observed",
        })
    assert failure.value.code == "REVISION_CONFLICT"
    assert harness.ctx.durable.entries(session) == before


INVALID_SECONDS: list[JsonValue] = [True, "10", -1, 0, 86401]


@pytest.mark.parametrize("seconds", INVALID_SECONDS)
def test_invalid_delegation_budget_does_not_create_a_conversation(harness: Harness, seconds: JsonValue) -> None:
    before = harness.ctx.durable.conversation_ids()
    with pytest.raises(ControlError) as failure:
        harness.service.execute(AGENT, "create_conversation", {
            "request_id": "invalid", "project": str(harness.project), "max_seconds": seconds,
        })
    assert failure.value.code == "INVALID_ARGUMENT"
    assert harness.ctx.durable.conversation_ids() == before


def test_health_and_scheduler_resume_preserve_the_scheduler_payload(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[bool] = []

    def resume() -> None:
        resumed.append(True)

    monkeypatch.setattr(harness.ctx.durable_scheduler, "resume", resume)
    expected = harness.ctx.durable_scheduler.health()
    health = harness.service.execute(AGENT, "get_health", {})
    assert health["scheduler"] == expected
    assert health["status"] == "ready"
    assert harness.service.execute(USER, "resume_scheduler", {}) == expected
    assert resumed == [True]


def test_release_dispatch_preserves_persistent_owner_fields(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.launcher import releases

    calls: list[str] = []

    def dispatch(owner: _ActivationOwner, name: str, args: dict[str, object]) -> dict[str, object]:
        assert owner.ctx.state_home == harness.ctx.state_home
        assert owner.instance_id == harness.service.instance_id
        assert owner.allowed_roots == harness.service.allowed_roots
        calls.append(name)
        return {"operation": name}

    monkeypatch.setattr(releases, "release_command", dispatch)
    commands: list[tuple[str, dict[str, object]]] = [
        ("prepare_release", {"request_id": "prepare", "source": str(harness.project)}),
        ("activate_release", {
            "request_id": "activate", "candidate_release": "release_fixture", "candidate_digest": "digest",
            "expected_instance_id": harness.service.instance_id,
        }),
        ("get_upgrade", {"upgrade_id": "upgrade_" + "a" * 32}),
    ]
    for name, params in commands:
        assert harness.service.execute(USER, name, params) == {"operation": name}
    assert harness.service.execute(AGENT, "get_upgrade", commands[-1][1]) == {"operation": "get_upgrade"}
    with pytest.raises(ControlError) as failure:
        harness.service.execute(AGENT, "prepare_release", commands[0][1])
    assert failure.value.code == "FORBIDDEN"
    assert calls == [name for name, _params in commands] + ["get_upgrade"]


@pytest.mark.parametrize("timeout", [-1, 31, 1e200, 10**1000])
def test_out_of_range_wait_bounds_remain_control_errors(harness: Harness, timeout: int | float) -> None:
    with pytest.raises(ControlError) as failure:
        harness.service.execute(AGENT, "wait_run", {
            "conversation_id": "unused", "run_id": "unused", "timeout_seconds": timeout,
        })
    assert failure.value.code == "INVALID_ARGUMENT"
    assert not harness.ctx.durable.tasks()
