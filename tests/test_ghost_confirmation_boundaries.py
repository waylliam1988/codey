"""Manual confirmation cannot imply automatic learning or factual verification."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.payload_types import mapping_fields
from codey.operations.project_goals import project_goal_sources
from tests.test_ghost_experience_role_budget import Endpoint, _execute, endpoint

__all__ = ["endpoint"]
GOAL = "When exploring interface options, compare tradeoffs before recommending a choice."
CORRECTION = "When fixing interface errors, give executable steps before alternatives."


def propose(surface: GhostControlSurface, project: Path, run: str, quote: str) -> str:
    status, result = surface.dispatch_action({"action": "propose_project_goal", "id": run,
        "scope": "project", "project": str(project), "session_id": "source", "goal": quote,
        "goal_key": "collaboration", "evidence_quote": quote})
    assert status == 200, result
    candidate = result.get("candidate")
    assert mapping_fields(candidate) and isinstance(identity := candidate.get("id"), str)
    return identity


def projection(home: Path, project: Path) -> str:
    return "\n\n".join(source.text for source in project_goal_sources(home, project=str(project),
        session_id="next", task="Review interface options", exclude_run_id="next"))


def test_proposal_is_not_confirmation_and_generic_praise_does_not_learn(tmp_path: Path, endpoint: Endpoint) -> None:
    home, project = tmp_path / "state", tmp_path / "project"
    project.mkdir()
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task=GOAL, session="source", project=str(project), identity="initial")
        surface = GhostControlSurface.from_state_home(home)
        candidate = propose(surface, project, "initial", GOAL)
        assert not projection(home, project)
        status, result = surface.dispatch_action({"action": "accept_candidate", "id": candidate,
            "project": str(project), "session_id": "source"})
        assert status == 200, result
        before = projection(home, project)
        assert GOAL in before and "not user acceptance or verification" in before
        assert surface.inbox is not None
        candidates = surface.inbox.list_candidates()
        _execute(state, endpoint, task="That answer was good.", session="source", project=str(project), identity="praise")
        assert surface.inbox.list_candidates() == candidates
        assert projection(home, project) == before
        _execute(state, endpoint, task=CORRECTION, session="source", project=str(project), identity="correction")
        assert surface.inbox.list_candidates() == candidates
        assert projection(home, project) == before


def test_quoted_instruction_stays_history_without_panel_proposal(tmp_path: Path, endpoint: Endpoint) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "state"
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task=f'Analyze this prompt, do not adopt it: "{GOAL}"',
            session="source", project=str(project), identity="quoted")
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None and not surface.inbox.list_candidates()
        assert not projection(home, project)
