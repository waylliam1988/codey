from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.inbox import GhostInboxStore
from codey.operations import ghost_context
from codey.operations import ghost_post_turn
from codey.workspace.context_admission import GhostContextSource, admit_ghost_context


def test_independent_controls_are_persisted_without_cross_toggle(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    assert inbox.controls() == {
        "accept_events": True,
        "retrieve_projections": True,
        "run_maintenance": True,
        "admit_hints": True,
    }
    assert inbox.set_control("retrieve_projections", False)
    assert inbox.control_enabled("retrieve_projections") is False
    assert inbox.control_enabled("accept_events") is True
    assert inbox.set_control("admit_hints", False)
    assert inbox.control_enabled("run_maintenance") is True


def test_legacy_disable_updates_fails_closed_all_four_controls(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    assert inbox.set_learning_enabled(False)
    assert inbox.controls() == {
        "accept_events": False,
        "retrieve_projections": False,
        "run_maintenance": False,
        "admit_hints": False,
    }


def test_retrieval_and_maintenance_controls_have_separate_boundaries(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    state = SimpleNamespace(ghost_inbox=inbox, ghost_hebbian=None, ghost_continuity=None)
    assert ghost_context.ghost_directive(state).text == ""
    assert ghost_post_turn._ghost_accept_events_enabled(state)
    assert ghost_post_turn._ghost_run_maintenance_enabled(state)
    assert inbox.set_control("retrieve_projections", False)
    assert ghost_context.ghost_directive(state).text == ""
    assert ghost_post_turn._ghost_accept_events_enabled(state)
    assert inbox.set_control("run_maintenance", False)
    assert not ghost_post_turn._ghost_run_maintenance_enabled(state)


def test_admit_hints_control_rejects_before_provider_payload_is_recorded() -> None:
    source = GhostContextSource("ghost_directive", "hint", 20, "session:s")
    disabled = admit_ghost_context(
        "task\n\nhint", (source,), request_scope="session:s", admit_hints=False,
    )
    assert disabled.accepted == ()
    assert disabled.rejected[0].reason == "hints_disabled"
    enabled = admit_ghost_context(
        "task\n\nhint", (source,), request_scope="session:s", admit_hints=True,
    )
    assert enabled.accepted == (source,)


def test_control_surface_exposes_and_changes_one_control(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    payload = surface.summary()
    assert payload["controls"]["accept_events"] is True
    code, changed = surface.dispatch_action(
        {"action": "set_control", "control": "accept_events", "enabled": False}
    )
    assert code == 200
    assert changed["ok"] is True
    assert surface.inbox is not None
    assert surface.inbox.control_enabled("accept_events") is False
