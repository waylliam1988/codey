"""Normal App entry, manual signal authority and physical-send lifecycle checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.payload_types import mapping_fields
from codey.ghost.project_goals import confirmed_project_goal_text, priority_text
from codey.operations import project_goals, task_loop
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.task_session import TaskSession
from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import ContextBudget
from tests.test_ghost_experience_role_budget import RESULT, Endpoint, _execute
from tests.test_ghost_experience_role_budget import endpoint as endpoint

INITIAL = "For storage design, working without a network matters most. Please remember this project goal."
CORRECTION = "For storage design, I now prioritize simultaneous collaboration. Please update this project goal."
MAINTAIN = "For storage design, I still prioritize simultaneous collaboration. Please maintain this project goal."
TASK = "Choose our new storage proposal. No changes required."


def normal(state: AppContext, capture: Endpoint, project: Path, *, task: str = TASK, identity: str,
           configure: Callable[[ApiProvider], None] | None = None, stop: str = "done",
           session: str = "new-session") -> None:
    def setup(provider: ApiProvider) -> None:
        provider._context_budget = ContextBudget(262144, 8192, 24576, 32000)
        if configure is not None:
            configure(provider)

    _execute(state, capture, task=task, session=session, project=str(project), intent="project", identity=identity,
             configure=setup, expected_stop=stop)


def propose(surface: GhostControlSurface, project: Path, *, run: str = "initial", quote: str = INITIAL,
            priority: str = "offline_access", reply: str = "", session: str = "new-session") -> str:
    status, payload = surface.dispatch_action({
        "action": "propose_project_goal", "id": run, "session_id": session, "project": str(project),
        "scope": "project", "priority": priority, "evidence_quote": quote,
        "reply_run_id": reply, "reply_digest": hashlib.sha256(RESULT.encode()).hexdigest() if reply else "",
    })
    assert status == 200 and payload["ok"], payload
    candidate = payload["candidate"]
    assert mapping_fields(candidate) and isinstance(candidate["id"], str)
    return candidate["id"]


def accept(surface: GhostControlSurface, project: Path, candidate_id: str, *, session: str = "new-session") -> None:
    status, payload = surface.dispatch_action({"action": "accept_candidate", "id": candidate_id,
                                             "scope": "project", "project": str(project), "session_id": session})
    assert status == 200 and payload["ok"], payload


def seed(state: AppContext, capture: Endpoint, project: Path) -> tuple[GhostControlSurface, str]:
    normal(state, capture, project, task=INITIAL, identity="initial")
    surface = GhostControlSurface.from_state_home(state.state_home)
    candidate_id = propose(surface, project)
    accept(surface, project, candidate_id)
    return surface, candidate_id


def projected(capture: Endpoint) -> bool:
    return "Manually confirmed current project goal" in json.dumps(capture.payloads[-1])


def rebuild(inbox: GhostInboxStore) -> None:
    with inbox.records.transaction():
        rebuilt = inbox._rebuild_candidates_from_events_unlocked()
        assert rebuilt is not None
        inbox._write_state(rebuilt)


def test_normal_new_session_initial_correction_maintenance_and_cold_replay(tmp_path: Path, endpoint: Endpoint) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    with closing(AppContext(home, start_scheduler=False)) as state:
        normal(state, endpoint, project, task=INITIAL, identity="initial")
        surface = GhostControlSurface.from_state_home(home)
        first = propose(surface, project)
        normal(state, endpoint, project, identity="before-review")
        assert not projected(endpoint), "a proposal is not a manually confirmed goal"
        accept(surface, project, first)
        assert surface.hebbian is not None and not surface.hebbian.list_nodes()
        assert not surface.summary(session_id="new-session", project=str(project))["repair"]
        normal(state, endpoint, project, identity="applied-initial", session="another-session")
        assert projected(endpoint) and priority_text("offline_access") in json.dumps(endpoint.payloads[-1])
        normal(state, endpoint, project, task=CORRECTION, identity="correction")
        second = propose(surface, project, run="correction", quote=CORRECTION,
                         priority="shared_editing", reply="applied-initial")
        accept(surface, project, second)
        normal(state, endpoint, project, identity="applied-correction")
        assert priority_text("shared_editing") in json.dumps(endpoint.payloads[-1])
        normal(state, endpoint, project, task=MAINTAIN, identity="maintenance")
        third = propose(surface, project, run="maintenance", quote=MAINTAIN,
                        priority="shared_editing", reply="applied-correction")
        accept(surface, project, third)
        assert surface.inbox is not None and surface.observations is not None
        before = confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project))
        assert all(quote in before[0] for quote in (INITIAL, CORRECTION, MAINTAIN))
        assert len(surface.inbox.list_candidates()) == 3, "maintenance needs its own attributable source"
        rebuild(surface.inbox)
        assert before == confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project))
    with closing(AppContext(home, start_scheduler=False)) as state:
        normal(state, endpoint, project, identity="cold-next")
        assert projected(endpoint) and MAINTAIN in json.dumps(endpoint.payloads[-1])
    assert len(endpoint.payloads) == 7, "Ghost adds no generation or retry"


@pytest.mark.parametrize("boundary", ("retrieve_off", "admit_off", "accept_off", "session_off", "other_project",
                                       "context", "reject", "delete_project", "delete_source_session", "reset"))
def test_normal_entry_controls_withdrawal_scope_and_persistent_deletion(
    tmp_path: Path, endpoint: Endpoint, boundary: str,
) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    with closing(AppContext(home, start_scheduler=False)) as state:
        surface, candidate_id = seed(state, endpoint, project)
        assert surface.inbox is not None
        if boundary in {"retrieve_off", "admit_off", "accept_off", "session_off"}:
            control = {"retrieve_off": "retrieve_projections", "admit_off": "admit_hints",
                       "accept_off": "accept_events", "session_off": "admit_hints"}[boundary]
            assert surface.inbox.set_control(control, False, scope="session" if boundary == "session_off" else "project",
                                             project=str(project), session_id="new-session")
        elif boundary == "reject":
            status, payload = surface.dispatch_action({"action": "reject_candidate", "id": candidate_id,
                                                       "scope": "project", "project": str(project)})
            assert status == 200 and payload["ok"], payload
        elif boundary in {"delete_project", "delete_source_session", "reset"}:
            action: dict[str, object] = {"action": "delete_scope", "scope": "project", "project": str(project), "confirm": True}
            if boundary == "delete_source_session":
                action.update(scope="session", session_id="new-session")
            elif boundary == "reset":
                action = {"action": "reset_all", "confirm": True}
            status, payload = surface.dispatch_action(action)
            assert status == 200 and payload["ok"], payload
            rebuild(surface.inbox)
            assert surface.hebbian is not None and surface.hebbian.rebuild_from_events()
    if boundary == "other_project":
        project = tmp_path / "other"
        project.mkdir()
    with closing(AppContext(home, start_scheduler=False)) as state:
        normal(state, endpoint, project, task="Give incident status. No changes required." if boundary == "context" else TASK,
               identity="next")
        assert projected(endpoint) == (boundary == "accept_off"), "update control does not revoke accepted retrieval"
    assert len(endpoint.payloads) == 2


@pytest.mark.parametrize("change", ("model", "temperature", "stream", "tools", "output", "window", "keep", "limit", "safety"))
def test_unverified_configuration_falls_back_but_keeps_local_goal(tmp_path: Path, endpoint: Endpoint, change: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        surface, _ = seed(state, endpoint, project)

        def configure(provider: ApiProvider) -> None:
            if change == "model":
                provider.model = "unverified"
            elif change == "temperature":
                provider.temperature = 0.2
            elif change == "stream":
                provider.stream = True
            elif change == "tools":
                provider.native_tools = False
            else:
                values = {"output": ContextBudget(262144, 128, 32640, 32000),
                          "window": ContextBudget(131072, 8192, 24576, 32000),
                          "keep": ContextBudget(262144, 8192, 24576, 16000),
                          "limit": ContextBudget(262144, 8192, 24576, 32000, input_limit_tokens=12000),
                          "safety": ContextBudget(262144, 8192, 10000, 32000)}
                provider._context_budget = values[change]

        normal(state, endpoint, project, identity="next", configure=configure)
        assert not projected(endpoint)
        assert surface.inbox is not None and len(surface.inbox.list_candidates(status="accepted")) == 1
    assert len(endpoint.payloads) == 2


@pytest.mark.parametrize("change", ("delete", "reject", "retrieve_off", "admit_off", "model", "budget"))
def test_physical_send_revalidates_goal_after_request_assembly(tmp_path: Path, endpoint: Endpoint, change: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        surface, candidate_id = seed(state, endpoint, project)
        assert surface.inbox is not None
        inbox = surface.inbox
        changed: list[str] = []

        def configure(provider: ApiProvider) -> None:
            def before_send(payload: dict[str, object]) -> None:
                if changed or "Manually confirmed current project goal" not in json.dumps(payload):
                    return
                changed.append(change)
                if change in {"delete", "reject"}:
                    body: dict[str, object] = {"action": "delete_scope", "scope": "project", "project": str(project), "confirm": True}
                    if change == "reject":
                        body = {"action": "reject_candidate", "id": candidate_id, "scope": "project", "project": str(project)}
                    status, result = surface.dispatch_action(body)
                    assert status == 200 and result["ok"], result
                elif change in {"retrieve_off", "admit_off"}:
                    assert inbox.set_control("retrieve_projections" if change == "retrieve_off" else "admit_hints", False)
                elif change == "model":
                    provider.model = "unverified"
                else:
                    provider._context_budget = ContextBudget(262144, 128, 32640, 32000)
            provider.configure_request = before_send

        normal(state, endpoint, project, identity="next", configure=configure, stop="error")
        assert changed == [change]
    assert len(endpoint.payloads) == 1, "stale goal must stop before physical HTTP"


@pytest.mark.parametrize("fault", ("controls", "observations"))
def test_goal_read_failure_is_isolated_from_normal_task(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        seed(state, endpoint, project)

        class BrokenInbox(GhostInboxStore):
            def control_enabled(self, name: str, *, project: str = "", session_id: str = "") -> bool:
                raise sqlite3.OperationalError("injected goal control failure")

        class BrokenObservations(GhostObservationStore):
            def read_committed(self, *, session_id: str = "", project: str = "", limit: int = 200,
                               scope: str = "session") -> tuple[dict[str, object], ...]:
                raise sqlite3.OperationalError("injected goal source failure")

        if fault == "controls":
            monkeypatch.setattr(project_goals, "GhostInboxStore", BrokenInbox)
        else:
            monkeypatch.setattr(project_goals, "GhostObservationStore", BrokenObservations)
        normal(state, endpoint, project, identity="next")
        assert not projected(endpoint)
    assert len(endpoint.payloads) == 2


def test_restored_source_cannot_borrow_a_fresh_confirmed_goal(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        seed(state, endpoint, project)
        captured: list[KernelRunRequest] = []
        original = task_loop.run_task_kernel

        def capture(session: TaskSession, *, request: KernelRunRequest) -> KernelResult:
            captured.append(request)
            return original(session, request=request)

        monkeypatch.setattr(task_loop, "run_task_kernel", capture)
        normal(state, endpoint, project, identity="capture")
        assert captured and captured[-1].transport.ghost_sources
        old = captured[-1].transport
        surface = GhostControlSurface.from_state_home(state.state_home)
        status, result = surface.dispatch_action({"action": "delete_scope", "scope": "project",
                                                  "project": str(project), "confirm": True})
        assert status == 200 and result["ok"], result
        normal(state, endpoint, project, task=CORRECTION, identity="fresh-source")
        candidate_id = propose(surface, project, run="fresh-source", quote=CORRECTION, priority="shared_editing")
        accept(surface, project, candidate_id)

        def restore(session: TaskSession, *, request: KernelRunRequest) -> KernelResult:
            assert request.transport.ghost_sources and request.transport.ghost_sources != old.ghost_sources
            transport = replace(request.transport, context_text=old.context_text, ghost_sources=old.ghost_sources)
            return original(session, request=replace(request, transport=transport))

        monkeypatch.setattr(task_loop, "run_task_kernel", restore)
        normal(state, endpoint, project, identity="restored", stop="error")
    assert len(endpoint.payloads) == 3


def test_concurrent_identical_manual_proposals_have_one_event_and_restart_review(tmp_path: Path, endpoint: Endpoint) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "state"
    with closing(AppContext(home, start_scheduler=False)) as state:
        normal(state, endpoint, project, task=INITIAL, identity="initial")

    def act(_index: int) -> int:
        surface = GhostControlSurface.from_state_home(home)
        status, _ = surface.dispatch_action({"action": "propose_project_goal", "id": "initial", "session_id": "new-session",
                                             "project": str(project), "scope": "project", "priority": "offline_access",
                                             "evidence_quote": INITIAL})
        return status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(act, range(2)))
    assert sorted(results) == [200, 400]
    surface = GhostControlSurface.from_state_home(home)
    assert surface.inbox is not None
    rows = surface.inbox.list_candidates()
    assert len(rows) == 1
    accept(surface, project, rows[0].id)
    with closing(AppContext(home, start_scheduler=False)) as state:
        normal(state, endpoint, project, identity="next")
        assert projected(endpoint)


def saved(store: GhostObservationStore, project: Path, run: str, text: str, *, assistant: str = RESULT) -> None:
    assert store.append_completed(run_id=run, session_id="s", project=str(project), mode="project",
                                  user_text=text, assistant_text=assistant, stop_reason="done")


@pytest.mark.parametrize("fault", ("scope", "priority", "id", "session_id", "evidence_quote", "reply_digest",
                                    "reply_run_id", "source_deleted", "updates_disabled"))
def test_manual_signal_rejects_bad_input_or_binding_atomically(tmp_path: Path, fault: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None and surface.inbox is not None
    project = tmp_path / "project"
    saved(surface.observations, project, "target", "Which proposal should we use?")
    saved(surface.observations, project, "source", CORRECTION)
    body: dict[str, object] = {"action": "propose_project_goal", "id": "source", "session_id": "s",
                               "project": str(project), "scope": "project", "priority": "shared_editing",
                               "evidence_quote": CORRECTION, "reply_run_id": "target",
                               "reply_digest": hashlib.sha256(RESULT.encode()).hexdigest()}
    changes: dict[str, object] = {"scope": "user", "priority": "invented", "id": "missing",
                                  "session_id": "other", "evidence_quote": "ungrounded words",
                                  "reply_digest": "forged", "reply_run_id": "source"}
    if fault == "source_deleted":
        surface.observations.delete_scope("project", project=str(project))
    elif fault == "updates_disabled":
        surface.inbox.set_control("accept_events", False, scope="project", project=str(project))
    else:
        body[fault] = changes[fault]
    before_events = surface.inbox.records.events()
    status, payload = surface.dispatch_action(body)
    assert status == 400 and payload["ok"] is False
    assert not surface.inbox.list_candidates()
    assert surface.inbox.records.events() == before_events, "failed binding validation must roll back new events too"


def test_later_review_cannot_resurrect_an_older_goal_and_rejected_sources_do_not_block_new_confirmation(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None and surface.inbox is not None
    project = tmp_path / "project"
    saved(surface.observations, project, "old", INITIAL)
    old = propose(surface, project, run="old", session="s")
    saved(surface.observations, project, "new", CORRECTION)
    new = propose(surface, project, run="new", quote=CORRECTION, priority="shared_editing", reply="old", session="s")
    accept(surface, project, new, session="s")
    status, payload = surface.dispatch_action({"action": "accept_candidate", "id": old, "scope": "project", "project": str(project)})
    assert status == 400 and payload["ok"] is False
    status, payload = surface.dispatch_action({"action": "reject_candidate", "id": new, "scope": "project", "project": str(project)})
    assert status == 200 and payload["ok"]
    assert confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project)) == ("", "")
    saved(surface.observations, project, "fresh", MAINTAIN)
    fresh = propose(surface, project, run="fresh", quote=MAINTAIN, priority="shared_editing", reply="new", session="s")
    accept(surface, project, fresh, session="s")
    assert priority_text("shared_editing") in confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project))[1]


@pytest.mark.parametrize("fault", ("source_mutated", "target_mutated", "target_deleted", "source_window", "budget"))
def test_source_and_target_authority_and_budget_rechecked_on_cold_projection(tmp_path: Path, fault: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None and surface.inbox is not None
    project = tmp_path / "project"
    actual = "x" * 3500 if fault == "budget" else RESULT
    saved(surface.observations, project, "target", "A proposal discussion", assistant=actual)
    saved(surface.observations, project, "source", CORRECTION)
    status, payload = surface.dispatch_action({"action": "propose_project_goal", "id": "source", "session_id": "s",
                                              "project": str(project), "scope": "project", "priority": "shared_editing",
                                              "evidence_quote": CORRECTION, "reply_run_id": "target",
                                              "reply_digest": hashlib.sha256(actual.encode()).hexdigest()})
    assert status == 200 and payload["ok"], payload
    candidate = payload["candidate"]
    assert mapping_fields(candidate) and isinstance(candidate["id"], str)
    accept(surface, project, candidate["id"], session="s")
    if fault == "source_mutated":
        saved(surface.observations, project, "source", CORRECTION + " Changed contents.")
    elif fault == "target_mutated":
        saved(surface.observations, project, "target", "A proposal discussion", assistant="changed")
    elif fault in {"target_deleted", "source_window"}:
        from codey.ghost.event_windows import reset_window

        with surface.observations.database.write_transaction() as connection:
            if fault == "target_deleted":
                connection.execute("INSERT INTO ghost_retired_inputs(kind,identity) VALUES('observation','target')")
            else:
                reset_window(connection, "observations")
    cold = GhostControlSurface.from_state_home(tmp_path)
    assert cold.inbox is not None and cold.observations is not None
    if fault in {"source_mutated", "target_mutated"}:
        with pytest.raises(ValueError, match="changed"):
            confirmed_project_goal_text(cold.inbox, cold.observations, project=str(project))
    else:
        assert confirmed_project_goal_text(cold.inbox, cold.observations, project=str(project)) == ("", "")


def test_fresh_confirmed_source_survives_retirement_of_an_older_source_session(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None and surface.inbox is not None
    project = tmp_path / "project"
    saved(surface.observations, project, "old", INITIAL)
    old = propose(surface, project, run="old", session="s")
    accept(surface, project, old, session="s")
    surface.observations.delete_scope("session", session_id="s")
    assert confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project)) == ("", "")
    saved(surface.observations, project, "new-target", "Discuss a new storage plan")
    saved(surface.observations, project, "fresh", MAINTAIN)
    fresh = propose(surface, project, run="fresh", quote=MAINTAIN, priority="shared_editing", reply="new-target", session="s")
    accept(surface, project, fresh, session="s")
    history, goal = confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project))
    assert MAINTAIN in history and INITIAL not in history and priority_text("shared_editing") in goal


@pytest.mark.parametrize("change", ("protocol", "remote_endpoint"))
def test_configuration_gate_rejects_other_wire_protocol_and_endpoint(tmp_path: Path, change: str) -> None:
    provider = ApiProvider("http://127.0.0.1:5001/v1", project_goals.VERIFIED_PROJECT_GOAL_MODEL,
                           native_tools=True, temperature=0, output_tokens=8192,
                           context_window_tokens=262144, context_reserve_tokens=32768, context_keep_recent_tokens=32000)
    try:
        assert project_goals._configuration_enabled(provider)
        if change == "protocol":
            provider.api_protocol = "openai-responses"
        else:
            provider.base_url = "https://unverified.example/v1"
        assert not project_goals._configuration_enabled(provider)
    finally:
        provider.close()
