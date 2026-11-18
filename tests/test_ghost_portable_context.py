"""Portable manual context uses real stores, normal entries and HTTP/SSE."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observations import GhostObservationStore
from codey.ghost.payload_types import mapping_fields
from codey.ghost.project_goals import GOAL_BUDGET, HISTORY_BUDGET, confirmed_project_goal_text
from codey.operations.kernel_contracts import KernelExecutionDeps
from codey.operations.personal_context import supplement_personal_context
from codey.operations.task_effects import KernelEffectSink
from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import ContextBudget
from codey.workspace.context_admission import request_scope
from tests.manual.ghost_memory_routing.portable_context_trial import GOAL as TRIAL_GOAL
from tests.manual.ghost_memory_routing.portable_context_trial import SEED_REPLY, seed
from tests.test_ghost_experience_role_budget import RESULT, Endpoint, _execute
from tests.test_ghost_experience_role_budget import endpoint as endpoint
from tests.test_project_goal_production import accept, rebuild, saved

WARM = "Please use a warm and considerate tone."
GOAL = "Keep this interface accessible to keyboard users."
CHANGED_GOAL = "Prioritize screen reader access for this interface."


def test_live_trial_seed_uses_durable_normal_entry_without_generation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    seed(tmp_path / "state", project)
    surface = GhostControlSurface.from_state_home(tmp_path / "state")
    assert surface.inbox is not None and surface.observations is not None
    rows = surface.observations.read_committed(project=str(project), scope="project")
    assert len(rows) == 1 and rows[0]["assistant_text"] == SEED_REPLY
    assert TRIAL_GOAL in confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project))[1]


def test_session_recall_filters_workspace_before_ranking(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    saved(store, first, "first", "quartz interface")
    saved(store, second, "second", "quartz interface")
    assert {row["run_id"] for row in store.read_committed(session_id="s")} == {"first", "second"}
    for project, expected in ((first, "first"), (second, "second")):
        assert [row["run_id"] for row in store.retrieve("quartz", session_id="s", project=str(project))] == [expected]
    assert not store.retrieve("quartz", session_id="s")


def candidate_id(payload: dict[str, object]) -> str:
    candidate = payload["candidate"]
    assert mapping_fields(candidate) and isinstance(candidate["id"], str)
    return candidate["id"]


def preference(surface: GhostControlSurface, project: Path, *, scope: str = "user", value: str = "warm",
               existing_run: str = "") -> str:
    assert surface.observations is not None
    run = existing_run or f"preference-{scope}"
    if not existing_run:
        saved(surface.observations, project, run, WARM)
    status, payload = surface.dispatch_action({
        "action": "propose_preference", "id": run, "session_id": "s", "project": str(project),
        "scope": scope, "conflict_key": "tone", "value_key": value, "evidence_quote": WARM,
    })
    assert status == 200 and payload["ok"], payload
    identity = candidate_id(payload)
    accept(surface, project, identity, session="s")
    return identity


def goal(surface: GhostControlSurface, project: Path, *, text: str = GOAL, run: str = "goal",
         key: str = "accessibility", reply: str = "", confirm: bool = True, existing: bool = False) -> str:
    assert surface.observations is not None
    if not existing:
        saved(surface.observations, project, run, text)
    status, payload = surface.dispatch_action({
        "action": "propose_project_goal", "id": run, "session_id": "s", "project": str(project),
        "scope": "project", "goal": text, "goal_key": key, "evidence_quote": text,
        "reply_run_id": reply, "reply_digest": hashlib.sha256(RESULT.encode()).hexdigest() if reply else "",
    })
    assert status == 200 and payload["ok"], payload
    identity = candidate_id(payload)
    if confirm:
        accept(surface, project, identity, session="s")
    return identity


def seed_portable(home: Path, project: Path, capture: Endpoint) -> GhostControlSurface:
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, capture, task=f"{WARM} {GOAL} quartzstore discussion", session="s",
                 project=str(project), identity="source")
        surface = GhostControlSurface.from_state_home(home)
        preference(surface, project, existing_run="source")
        goal(surface, project, run="source", existing=True)
    capture.payloads.clear()
    capture.paths.clear()
    capture.streams.clear()
    return surface


@pytest.mark.parametrize("protocol", ("openai-completions", "openai-responses"))
@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize("budget", (128, 4096, 8192))
def test_normal_writer_portable_context_on_real_wire(
    tmp_path: Path, endpoint: Endpoint, protocol: str, stream: bool, budget: int,
) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    seed_portable(home, project, endpoint)

    def configure(provider: ApiProvider) -> None:
        provider.model = "a-different-provider-model"
        provider.stream = stream
        provider._context_budget = ContextBudget(262144, budget, 32768 - budget, 32000)

    task = "Recall quartzstore and evaluate our interface. Use a direct tone for this turn. No changes required."
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task=task, session="new-session", project=str(project),
                 intent="project", identity="next", configure=configure, api_protocol=protocol)
    assert len(endpoint.payloads) == 1, "Ghost adds zero generation requests"
    sent = json.dumps(endpoint.payloads[0])
    assert "tone = warm and considerate" in sent and GOAL in sent and RESULT in sent
    assert task in sent and "override" in sent
    assert endpoint.paths == (["/v1/responses"] if protocol == "openai-responses" else ["/v1/chat/completions"])
    assert endpoint.streams == [stream]


@pytest.mark.parametrize("consumer", ("chat", "planning"))
def test_user_preference_crosses_projects_and_sessions(tmp_path: Path, endpoint: Endpoint, consumer: str) -> None:
    home, project, other = tmp_path / "state", tmp_path / "source", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    seed_portable(home, project, endpoint)
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task="Explain the interface. No changes required.", session="different-session",
                 project=str(other), intent=consumer, identity="other")
    sent = json.dumps(endpoint.payloads[-1])
    assert "tone = warm and considerate" in sent
    assert GOAL not in sent, "project goals cannot cross projects"
    assert len(endpoint.payloads) == 1


def test_project_preference_overrides_user_without_leaking(tmp_path: Path, endpoint: Endpoint) -> None:
    home, project, other = tmp_path / "state", tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    surface = seed_portable(home, project, endpoint)
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task=WARM, session="s", project=str(project), identity="project-source")
        preference(surface, project, scope="project", value="technical", existing_run="project-source")
        endpoint.payloads.clear()
        for index, path in enumerate((project, other)):
            _execute(state, endpoint, task="Explain the interface. No changes required.", session=f"new-{index}",
                     project=str(path), intent="project", identity=f"run-{index}")
    first, second = (json.dumps(payload) for payload in endpoint.payloads)
    assert "tone = technical" in first and "tone = warm and considerate" not in first
    assert "tone = warm and considerate" in second and "tone = technical" not in second


@pytest.mark.parametrize("boundary", ("enabled", "disabled", "deleted", "rejected"))
def test_research_builder_uses_same_local_authority_and_rechecks(tmp_path: Path, endpoint: Endpoint, boundary: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "state"
    surface = seed_portable(home, project, endpoint)
    assert surface.inbox is not None
    identity = next(row.id for row in surface.inbox.list_candidates() if row.signal_kind == "style_preference")
    with closing(AppContext(home, start_scheduler=False)) as state:
        sink = KernelEffectSink(state.runtime_mutations, session_id="next", run_id="research",
                                provider_id="fixture", managed_outputs=state.managed_outputs, durable=state.durable)
        execution = KernelExecutionDeps(intent_sink=sink, project_path=project, permission_profile="research")
        personal = supplement_personal_context(text="Research question", sources=(),
            scope=request_scope(project, "next"), run_id="research", task="Review accessibility research",
            enabled=True, execution=execution, validate=None)
        assert {source.key for source in personal.sources} == {
            "ghost_directive", "ghost_project_goal_history", "ghost_confirmed_project_goal"}
        assert personal.validate is not None and personal.validate(personal.sources)
        assert "tone = warm and considerate" in personal.text and GOAL in personal.text
        assert surface.inbox is not None
        if boundary == "disabled":
            assert surface.inbox.set_control("admit_hints", False)
        elif boundary == "deleted":
            status, payload = surface.dispatch_action({"action": "reset_all", "confirm": True})
            assert status == 200 and payload["ok"], payload
        elif boundary == "rejected":
            status, payload = surface.dispatch_action({"action": "reject_candidate", "id": identity})
            assert status == 200 and payload["ok"], payload
        assert personal.validate(personal.sources) == (boundary == "enabled")
        assert not personal.validate(())
        assert supplement_personal_context(text="base", sources=(), scope=request_scope(project, "next"),
            run_id="research", task="question", enabled=False, execution=execution, validate=None).text == "base"
        assert not supplement_personal_context(text="base", sources=(), scope="wrong-scope",
            run_id="research", task="question", enabled=True, execution=execution, validate=None).sources


def test_generic_goal_review_correction_independent_keys_and_cold_replay(tmp_path: Path) -> None:
    project = tmp_path / "project"
    surface = GhostControlSurface.from_state_home(tmp_path / "state")
    initial = goal(surface, project, confirm=False)
    assert surface.inbox is not None and surface.observations is not None
    assert confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project)) == ("", "")
    accept(surface, project, initial, session="s")
    goal(surface, project, text=CHANGED_GOAL, run="correction", reply="goal")
    goal(surface, project, text="Keep builds reproducible.", run="build", key="build")
    rebuild(surface.inbox)
    cold = GhostControlSurface.from_state_home(tmp_path / "state")
    assert cold.inbox is not None and cold.observations is not None
    history, current = confirmed_project_goal_text(cold.inbox, cold.observations, project=str(project))
    assert CHANGED_GOAL in current and GOAL not in current
    assert "Keep builds reproducible." in current
    assert GOAL in history and RESULT in history
    assert len(history) <= HISTORY_BUDGET and len(current) <= GOAL_BUDGET
    summary = cold.summary(project=str(project), session_id="new-session")
    observations = summary["observations"]
    assert isinstance(observations, list) and len(observations) == 3
    rows: list[object] = observations
    assert all(mapping_fields(row) and row["session_id"] == "s"
               and row["reply_digest"] == hashlib.sha256(RESULT.encode()).hexdigest() for row in rows)


def test_many_goals_admit_complete_newest_pairs_instead_of_disappearing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    surface = GhostControlSurface.from_state_home(tmp_path)
    for index in range(6):
        goal(surface, project, text=f"Goal {index}: keep interface access predictable.", run=f"goal-{index}", key=f"key_{index}")
    assert surface.inbox is not None and surface.observations is not None
    history, current = confirmed_project_goal_text(surface.inbox, surface.observations, project=str(project))
    assert "Goal 5:" in current and "Goal 5:" in history
    assert len(history) <= HISTORY_BUDGET and len(current) <= GOAL_BUDGET


@pytest.mark.parametrize("field,value", (("goal", "invented goal"), ("scope", "user"),
                                        ("goal_key", "bad key"), ("reply_digest", "forged")))
def test_generic_goals_reject_untrusted_fields(tmp_path: Path, field: str, value: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = tmp_path / "project"
    assert surface.observations is not None and surface.inbox is not None
    saved(surface.observations, project, "source", GOAL)
    body: dict[str, object] = {"action": "propose_project_goal", "id": "source", "session_id": "s",
        "project": str(project), "scope": "project", "goal": GOAL, "goal_key": "focus", "evidence_quote": GOAL}
    body[field] = value
    before = surface.inbox.records.events()
    status, payload = surface.dispatch_action(body)
    assert status == 400 and payload["ok"] is False
    assert surface.inbox.records.events() == before
