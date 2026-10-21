"""The scoped production control reader is the only authority."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.ghost.inbox import GhostInboxStore
from codey.operations.ghost_context import ghost_control_enabled
from codey.operations.ghost_post_turn import _ghost_accept_events_enabled
from codey.operations.task_phases.settlement import _persist_observation_row

CONTROLS = ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints")


@pytest.mark.parametrize("control", CONTROLS)
def test_scoped_type_error_never_retries_without_scope(control):
    calls = []

    def read(name, **scope):
        calls.append((name, scope))
        if scope:
            raise TypeError("scoped settings decode failed")
        return True

    state = SimpleNamespace(ghost_inbox=SimpleNamespace(control_enabled=read))
    assert ghost_control_enabled(state, control, project="project-a", session_id="session-a") is False
    assert calls == [(control, {"project": "project-a", "session_id": "session-a"})]


@pytest.mark.parametrize("control", CONTROLS)
def test_name_only_control_reader_is_not_a_supported_contract(control):
    state = SimpleNamespace(ghost_inbox=SimpleNamespace(control_enabled=lambda name: True))
    assert ghost_control_enabled(state, control, project="project-a", session_id="session-a") is False


@pytest.mark.parametrize("control", CONTROLS)
def test_legacy_learning_reader_cannot_authorize_independent_control(control):
    calls = []
    state = SimpleNamespace(ghost_inbox=SimpleNamespace(learning_enabled=lambda: calls.append("legacy") or True))
    assert ghost_control_enabled(state, control, project="project-a", session_id="session-a") is False
    assert calls == []


@pytest.mark.parametrize("control", CONTROLS)
@pytest.mark.parametrize("enabled", [False, True])
def test_production_scoped_contract_preserves_independent_bool(control, enabled):
    calls = []

    def read(name, *, project, session_id):
        calls.append((name, project, session_id))
        return enabled

    state = SimpleNamespace(ghost_inbox=SimpleNamespace(control_enabled=read))
    assert ghost_control_enabled(state, control, project="project-a", session_id="session-a") is enabled
    assert calls == [(control, "project-a", "session-a")]


def _settle(state, *, project="P:/alpha", session_id="s1"):
    return _persist_observation_row(
        state, run_id="control-probe", session_id=session_id, project=project, mode="chat",
        user_text="synthetic task", assistant_text="synthetic answer", stop_reason="done", provider_id="fixture",
    )


@pytest.mark.parametrize("scope", ["project", "session"])
def test_observation_settlement_uses_scoped_accept_events(tmp_path, scope):
    inbox = GhostInboxStore(tmp_path)
    assert inbox.set_control("accept_events", False, scope=scope, project="P:/alpha", session_id="s1")
    store = SimpleNamespace(append_completed=Mock(return_value=True))
    state = SimpleNamespace(ghost_inbox=inbox, ghost_observations=store)
    assert _settle(state) == (True, "skipped")
    store.append_completed.assert_not_called()
    # A separate scope remains enabled; a scoped deny must not disable all writes.
    assert _settle(state, project="P:/beta", session_id="s2") == (True, "committed")
    assert store.append_completed.call_count == 1


def test_post_turn_requires_control_authority():
    assert _ghost_accept_events_enabled(SimpleNamespace()) is False


def test_observation_settlement_requires_control_authority():
    store = SimpleNamespace(append_completed=Mock(return_value=True))
    assert _settle(SimpleNamespace(ghost_observations=store)) == (True, "skipped")
    store.append_completed.assert_not_called()


def test_observation_settlement_does_not_use_legacy_learning_reader():
    store = SimpleNamespace(append_completed=Mock(return_value=True))
    inbox = SimpleNamespace(learning_enabled=lambda: True)
    assert _settle(SimpleNamespace(ghost_inbox=inbox, ghost_observations=store)) == (False, "settings_error")
    store.append_completed.assert_not_called()
