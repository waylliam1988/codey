"""Maintenance may refresh stored state but must honor external-source controls."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.runs.ledger_projection import ProviderFailureSummary, RunLedgerProjection
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed


@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("control", ["on", "off", "missing", "error"])
def test_sleep_external_sources_require_current_scoped_accept_events(
    tmp_path: Path, scope: str, control: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    assert surface.sleep is not None
    assert surface.affinity is not None
    assert surface.continuity is not None
    project = str(tmp_path / "project")
    assert surface.inbox.set_control(
        "accept_events", control != "off", scope=scope, project=project, session_id="sleep-session",
    )
    projection = RunLedgerProjection(
        run_id="sleep-source", session_id="sleep-session", project=project, mode="chat",
        provider_failures=(ProviderFailureSummary("local", "send", "timeout", "transport", "fixture failure"),),
    )
    with patch.object(
        surface.inbox, "control_enabled", side_effect=ValueError("unreadable settings"),
    ) if control == "error" else nullcontext():
        report = surface.sleep.run_once(
            inbox_store=None if control == "missing" else surface.inbox,
            continuity_store=surface.continuity, affinity_store=surface.affinity,
            run_projection=projection, run_id=projection.run_id,
            project=project, session_id="sleep-session",
        )
    continuity = surface.continuity.list_items()
    affinity = surface.affinity.list_nodes(project=project, session_id="sleep-session")
    assert bool(continuity) == (control == "on")
    assert bool(affinity) == (control == "on")
    assert report.steps[-1].ok
    if control in {"missing", "error"}:
        assert report.warnings
    else:
        assert not report.warnings


@pytest.mark.parametrize("control", ["retrieve_projections", "admit_hints"])
def test_sleep_event_ingestion_is_independent_of_retrieval_and_hint_admission(tmp_path: Path, control: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    assert surface.sleep is not None
    assert surface.continuity is not None
    assert surface.inbox.set_control(control, False)
    projection = RunLedgerProjection(run_id="sleep-source", project=str(tmp_path / "project"), mode="chat")
    report = surface.sleep.run_once(
        inbox_store=surface.inbox, continuity_store=surface.continuity, run_projection=projection,
    )
    assert report.steps[-1].ok
    assert surface.continuity.list_items()


def test_disabling_new_sources_keeps_maintenance_of_existing_accepted_state(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    _seed(surface, project)
    assert surface.inbox is not None
    assert surface.sleep is not None
    assert surface.continuity is not None
    assert surface.affinity is not None
    assert surface.inbox.set_control("accept_events", False, scope="project", project=project)
    report = surface.sleep.run_once(
        inbox_store=surface.inbox, hebbian_store=surface.hebbian,
        affinity_store=surface.affinity, continuity_store=surface.continuity, project=project,
    )
    assert report.steps[-1].ok
    assert surface.continuity.list_items()
    assert surface.affinity.list_nodes(project=project)
