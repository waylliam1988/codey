"""Deletion responses must reflect each production store's actual outcome."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.payload_types import mapping_fields

STORE_NAMES = ("inbox", "hebbian", "continuity", "sleep", "work_queue", "affinity", "observations")


def _stores(surface: GhostControlSurface) -> dict[str, object]:
    stores: dict[str, object] = {
        "inbox": surface.inbox,
        "hebbian": surface.hebbian,
        "continuity": surface.continuity,
        "sleep": surface.sleep,
        "work_queue": surface.work_queue,
        "affinity": surface.affinity,
        "observations": surface.observations,
    }
    assert all(store is not None for store in stores.values())
    return stores


def _request(action: str) -> dict[str, object]:
    return {"action": action, "confirm": True, "scope": "project", "project": "control-project"}


def _assert_failed_store(
    response: tuple[int, dict[str, object]], store_name: str, action: str,
) -> None:
    status, payload = response
    assert status == 500
    assert payload["ok"] is False
    assert payload["errors"] == [f"{store_name}_{action}_failed"]
    results = payload["results"]
    assert mapping_fields(results)
    assert set(results) == set(STORE_NAMES)
    assert results[store_name] is False


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("store_name", STORE_NAMES)
def test_explicit_store_failure_is_not_reported_as_success(
    tmp_path: Path, action: str, store_name: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    with patch.object(_stores(surface)[store_name], action, return_value=False):
        _assert_failed_store(surface.dispatch_action(_request(action)), store_name, action)


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("store_name", STORE_NAMES)
def test_sqlite_store_failure_is_structured_and_other_stores_are_attempted(
    tmp_path: Path, action: str, store_name: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    with patch.object(
        _stores(surface)[store_name], action, side_effect=sqlite3.OperationalError("database is locked"),
    ):
        _assert_failed_store(surface.dispatch_action(_request(action)), store_name, action)


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("error", [OSError, TypeError, ValueError])
def test_existing_store_exception_contract_remains_structured(
    tmp_path: Path, action: str, error: type[Exception],
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    with patch.object(surface.inbox, action, side_effect=error("store failed")):
        _assert_failed_store(surface.dispatch_action(_request(action)), "inbox", action)


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
def test_normal_empty_store_results_and_observation_none_are_successful(tmp_path: Path, action: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    status, payload = surface.dispatch_action(_request(action))
    assert status == 200
    assert payload["ok"] is True
    assert payload["errors"] == []
    results = payload["results"]
    assert mapping_fields(results)
    assert set(results) == set(STORE_NAMES)
    if action == "delete_scope":
        assert results["observations"] == 0
        assert results["observations"] is not False
    else:
        assert results["observations"] is None


def test_partial_surface_retains_explicit_unavailable_results(tmp_path: Path) -> None:
    surface = GhostControlSurface(inbox=GhostInboxStore(tmp_path))
    status, payload = surface.dispatch_action(_request("reset_all"))
    assert status == 200
    assert payload["ok"] is True
    results = payload["results"]
    assert mapping_fields(results)
    assert results["observations"] == "unavailable"


def test_reset_keeps_explicit_user_disable(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    assert surface.inbox.set_control("admit_hints", False)
    status, payload = surface.dispatch_action(_request("reset_all"))
    assert status == 200
    assert payload["ok"] is True
    assert not GhostInboxStore(tmp_path).control_enabled("admit_hints")


def test_failure_does_not_prevent_other_real_stores_from_deleting(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None
    assert surface.observations.append_completed(
        run_id="control-run", session_id="control-session", project="control-project", mode="chat",
        user_text="Save this request", assistant_text="Saved", stop_reason="done",
    )
    assert surface.observations.read_all()
    with patch.object(surface.inbox, "delete_scope", return_value=False):
        response = surface.dispatch_action(_request("delete_scope"))
    assert surface.observations.read_all() == ()
    _assert_failed_store(response, "inbox", "delete_scope")


def test_multiple_failures_are_reported_without_hiding_later_stores(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    with (
        patch.object(surface.inbox, "reset_all", return_value=False),
        patch.object(surface.hebbian, "reset_all", side_effect=sqlite3.DatabaseError("broken database")),
    ):
        status, payload = surface.dispatch_action(_request("reset_all"))
    assert status == 500
    assert payload["ok"] is False
    assert payload["errors"] == ["inbox_reset_all_failed", "hebbian_reset_all_failed"]
    results = payload["results"]
    assert mapping_fields(results)
    assert results["continuity"] is True
    assert results["observations"] is None


def test_sqlite_control_update_error_is_a_structured_dispatch_failure(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    with patch.object(surface.inbox, "set_control", side_effect=sqlite3.DatabaseError("broken database")):
        status, payload = surface.dispatch_action({
            "action": "set_control", "control": "admit_hints", "enabled": False,
        })
    assert status == 500
    assert payload["ok"] is False
    assert payload["error"] == "broken database"


def test_action_body_requires_string_keys_before_dispatch(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    body: dict[object, object] = {"action": "reset_all", "confirm": True, 42: "invalid key"}
    status, payload = surface.dispatch_action(body)
    assert status == 400
    assert payload["ok"] is False
    assert payload["error"] == "invalid request"
