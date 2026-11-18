"""Project experience defaults, fallback and revocation at physical HTTP."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observation_index import RETRIEVED_BLOCK_HEADER, render_retrieved_block
from codey.ghost.observations import GhostObservationStore
from codey.operations import project_experiences, task_loop
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.task_session import TaskSession
from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import ContextBudget
from codey.workspace.context_admission import sources_from_values
from tests.test_ghost_experience_role_budget import RESULT, USER, Endpoint, _execute
from tests.test_ghost_experience_role_budget import endpoint as endpoint


def _seed(state: AppContext, project: Path, capture: Endpoint) -> None:
    _execute(state, capture, task=USER, session="source-session", project=str(project), identity="source")
    store = state.ghost_observations
    assert store is not None
    assert any(row["run_id"] == "source" and row["assistant_text"] == RESULT
               for row in store.read_committed(session_id="source-session", project=str(project), scope="project"))


def _change_config(provider: ApiProvider, change: str) -> None:
    if change == "model":
        provider.model = "unverified-model"
    elif change == "temperature":
        provider.temperature = 0.7
    elif change == "stream":
        provider.stream = True
    elif change == "protocol":
        provider.api_protocol = "openai-responses"
    elif change == "tools":
        provider.native_tools = False
    elif change == "budget":
        provider._context_budget = ContextBudget(32768, 128, 8064, 12000)
    elif change == "output":
        provider._context_budget = ContextBudget(262144, 256, 32512, 32000)
    else:
        raise AssertionError(change)


@pytest.mark.parametrize("change", ("model", "temperature", "stream", "tools", "budget", "output"))
def test_unverified_configuration_retains_history_but_omits_project_projection(
    tmp_path: Path, endpoint: Endpoint, change: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        _seed(state, project, endpoint)

        def configure(provider: ApiProvider) -> None:
            _change_config(provider, change)

        _execute(state, endpoint, task="Recall the quartzstore decision. No changes required.",
                 session="new-session", project=str(project), intent="project", identity="next", configure=configure)
        assert state.ghost_observations is not None
        assert any(row["run_id"] == "source" for row in state.ghost_observations.read_all())
    assert len(endpoint.payloads) == 2
    assert RETRIEVED_BLOCK_HEADER not in json.dumps(endpoint.payloads[-1])


def test_actual_inbox_control_sqlite_failure_does_not_abort_project_settlement(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        _seed(state, project, endpoint)

        def broken_settings(self: GhostInboxStore) -> dict[str, object]:
            raise sqlite3.OperationalError("injected settings query failure")

        monkeypatch.setattr(GhostInboxStore, "_read_settings_unlocked", broken_settings)
        _execute(state, endpoint, task="Recall the quartzstore decision. No changes required.",
                 session="new-session", project=str(project), intent="project", identity="next")
    assert len(endpoint.payloads) == 2
    assert RETRIEVED_BLOCK_HEADER not in json.dumps(endpoint.payloads[-1])


@pytest.mark.parametrize("boundary", ("retrieve_off", "deleted", "model", "different_current", "same_current"))
def test_restored_project_source_requires_current_entry_validation(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    with closing(AppContext(home, start_scheduler=False)) as state:
        _seed(state, project, endpoint)
        assert state.ghost_observations is not None
        history = render_retrieved_block(state.ghost_observations.read_committed(
            project=str(project), scope="project",
        ))
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None
        if boundary == "retrieve_off":
            assert surface.inbox.set_control("retrieve_projections", False, scope="project", project=str(project))
        elif boundary in {"deleted", "different_current"}:
            status, result = surface.dispatch_action({"action": "delete_scope", "scope": "session",
                                                     "session_id": "source-session", "confirm": True})
            assert status == 200, result
            if boundary == "different_current":
                _execute(state, endpoint, task="quartzstore fresh history context", session="source-session",
                         project=str(project), identity="fresh")
        original = task_loop.run_task_kernel
        restored: list[bool] = []

        def restore_prior_request(session: TaskSession, *, request: KernelRunRequest) -> KernelResult:
            # Fault injection models a receipt restoring old context/sources
            # after the new adapter assembled an empty projection. The shared
            # kernel and physical provider gate remain real.
            assert bool(request.transport.ghost_sources) == (boundary in {"different_current", "same_current"})
            assert all((source.text == history) == (boundary == "same_current")
                       for source in request.transport.ghost_sources)
            restored.append(True)
            sources = sources_from_values(experiences=history, scope=request.transport.context_scope)
            transport = replace(request.transport, ghost_sources=sources, context_text=history)
            return original(session, request=replace(request, transport=transport))

        def configure(provider: ApiProvider) -> None:
            if boundary == "model":
                _change_config(provider, "model")

        monkeypatch.setattr(task_loop, "run_task_kernel", restore_prior_request)
        event = _execute(state, endpoint, task="Recall the quartzstore decision. No changes required.",
                         session="new-session", project=str(project), intent="project", identity="next",
                         configure=configure, expected_stop="done" if boundary == "same_current" else "error")
        if boundary != "same_current":
            assert "Ghost context admission rejected" in str(event)
    assert restored == [True]
    assert len(endpoint.payloads) == (2 if boundary in {"different_current", "same_current"} else 1)


@pytest.mark.parametrize("change", (
    "delete", "retrieve_off", "admit_off", "model", "temperature", "stream", "protocol", "tools", "budget", "output",
))
def test_project_history_is_rechecked_after_assembly_before_physical_send(
    tmp_path: Path, endpoint: Endpoint, change: str,
) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    changed: list[str] = []
    with closing(AppContext(home, start_scheduler=False)) as state:
        _seed(state, project, endpoint)
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None
        inbox = surface.inbox

        def configure(provider: ApiProvider) -> None:
            def after_assembly(payload: dict[str, object]) -> None:
                if changed or RETRIEVED_BLOCK_HEADER not in json.dumps(payload):
                    return
                changed.append(change)
                if change == "delete":
                    status, result = surface.dispatch_action({"action": "delete_scope", "scope": "session",
                                                             "session_id": "source-session", "confirm": True})
                    assert status == 200, result
                elif change in {"retrieve_off", "admit_off"}:
                    control = "retrieve_projections" if change == "retrieve_off" else "admit_hints"
                    assert inbox.set_control(control, False, scope="project", project=str(project))
                else:
                    _change_config(provider, change)
            provider.configure_request = after_assembly

        event = _execute(state, endpoint, task="Recall the quartzstore decision. No changes required.",
                 session="new-session", project=str(project), intent="project", identity="next",
                 configure=configure, expected_stop="error")
        assert "Ghost context admission rejected" in str(event)
    assert changed == [change]
    assert len(endpoint.payloads) == 1, "a stale history request must stop before physical HTTP"


@pytest.mark.parametrize("fault", ("controls", "observations"))
def test_project_history_read_failure_falls_back_without_extra_request(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        _seed(state, project, endpoint)

        class BrokenInbox(GhostInboxStore):
            def control_enabled(self, name: str, *, project: str = "", session_id: str = "") -> bool:
                raise sqlite3.OperationalError("injected control read fault")

        class BrokenObservations(GhostObservationStore):
            def read_committed(
                self, *, session_id: str = "", project: str = "", limit: int = 200, scope: str = "session",
            ) -> tuple[dict[str, object], ...]:
                raise sqlite3.OperationalError("injected history read fault")

        if fault == "controls":
            monkeypatch.setattr(project_experiences, "GhostInboxStore", BrokenInbox)
        else:
            monkeypatch.setattr(project_experiences, "GhostObservationStore", BrokenObservations)
        _execute(state, endpoint, task="Recall the quartzstore decision. No changes required.",
                 session="new-session", project=str(project), intent="project", identity="next")
    assert len(endpoint.payloads) == 2
    assert RETRIEVED_BLOCK_HEADER not in json.dumps(endpoint.payloads[-1])
