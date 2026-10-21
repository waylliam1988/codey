from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost import _common
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
    assert inbox.set_control("accept_events", True, scope="user")
    assert inbox.control_enabled("accept_events") is True


def test_control_scope_inheritance_only_allows_narrower_deny(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    assert inbox.set_control("retrieve_projections", False, scope="user")
    assert inbox.set_control(
        "retrieve_projections", True, scope="project", project="P:/alpha",
    )
    assert not inbox.control_enabled("retrieve_projections", project="P:/alpha")
    assert not inbox.control_enabled("retrieve_projections", project="P:/other")

    assert inbox.set_control("retrieve_projections", True, scope="user")
    assert inbox.set_control(
        "retrieve_projections", False, scope="project", project="P:/alpha",
    )
    assert not inbox.control_enabled("retrieve_projections", project="P:/alpha")
    assert inbox.control_enabled("retrieve_projections", project="P:/other")
    assert inbox.set_control(
        "retrieve_projections", True, scope="session", session_id="s1",
        project="P:/alpha",
    )
    assert not inbox.control_enabled(
        "retrieve_projections", project="P:/alpha", session_id="s1",
    )


def test_scoped_controls_survive_restart_and_concurrent_writes(tmp_path: Path) -> None:
    first = GhostInboxStore(tmp_path)
    second = GhostInboxStore(tmp_path)

    def write(project: str) -> bool:
        return second.set_control(
            "admit_hints", False, scope="project", project=project,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(write, ("P:/one", "P:/two")))
    restarted = GhostInboxStore(tmp_path)
    assert not restarted.control_enabled("admit_hints", project="P:/one")
    assert not restarted.control_enabled("admit_hints", project="P:/two")
    assert restarted.control_enabled("admit_hints", project="P:/three")


def test_invalid_scope_reference_fails_closed_without_mutation(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    try:
        inbox.set_control("accept_events", False, scope="project")
    except ValueError:
        pass
    else:
        raise AssertionError("missing project must be rejected")
    assert inbox.control_enabled("accept_events")


def test_scope_delete_removes_control_override_and_survives_restart(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    assert inbox.set_control(
        "admit_hints", False, scope="project", project="P:/deleted",
    )
    assert not inbox.control_enabled("admit_hints", project="P:/deleted")
    assert inbox.delete_scope("project", project="P:/deleted") == 0
    restarted = GhostInboxStore(tmp_path)
    assert restarted.control_enabled("admit_hints", project="P:/deleted")


def test_user_scope_delete_restores_all_controls(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    assert inbox.set_learning_enabled(False)
    assert inbox.delete_scope("user") == 0
    assert inbox.controls() == {
        "accept_events": True,
        "retrieve_projections": True,
        "run_maintenance": True,
        "admit_hints": True,
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


def test_control_surface_scoped_action_and_export_are_replayable(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    code, changed = surface.dispatch_action({
        "action": "set_control",
        "control": "retrieve_projections",
        "enabled": False,
        "scope": "project",
        "project": "P:/scoped",
    })
    assert code == 200
    assert changed["scope"] == "project"
    assert surface.inbox is not None
    assert not surface.inbox.control_enabled("retrieve_projections", project="P:/scoped")
    exported = surface.export_state()
    settings = exported["inbox"]["settings"]
    project_ref = _common.normalize_project("P:/scoped")
    assert settings["control_scopes"]["project"][project_ref]["retrieve_projections"] is False
    assert surface.inbox.delete_scope("project", project="P:/scoped") == 0
    assert surface.inbox.control_enabled("retrieve_projections", project="P:/scoped")
