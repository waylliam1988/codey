"""The ordinary Ghost control surface durably schedules deletes by default."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import pytest

from codey.app.api import ghost_control_surface
from codey.app.context import AppContext
from codey.app.server import CodeyHTTPServer, Handler
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import (
    MAX_STORE_DETAIL_CHARS,
    DeletionManifest,
    DeletionManifestError,
    new_manifest,
    store_event,
)
from codey.ghost.deletion_manifest_coordinator import GHOST_STORE_NAMES
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.payload_types import mapping_fields
from codey.ghost.sqlite import GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed
from tests.test_ghost_received_input_lifetime import _receive, _run

CASES = (("delete_scope", "user"), ("delete_scope", "project"), ("delete_scope", "session"), ("reset_all", "all"))


def _request(project: str, *, action: str = "delete_scope", scope: str = "project", identity: str = "") -> dict[str, object]:
    body: dict[str, object] = {
        "action": action, "scope": scope, "project": project, "session_id": "received-session", "confirm": True,
    }
    if identity:
        body["delete_operation_id"] = identity
    return body


def _manifest(payload: dict[str, object]) -> DeletionManifest:
    manifest = DeletionManifest.from_payload(payload["deletion_manifest"])
    assert payload["delete_operation_id"] == manifest.operation_id
    return manifest


def _stores(surface: GhostControlSurface) -> dict[str, object]:
    return {
        "inbox": surface.inbox, "hebbian": surface.hebbian, "continuity": surface.continuity,
        "sleep": surface.sleep, "work_queue": surface.work_queue, "affinity": surface.affinity,
        "observations": surface.observations,
    }


@pytest.mark.parametrize("action,scope", CASES)
def test_actual_application_surface_defaults_to_manifest_and_retry_keeps_fresh_input(
    tmp_path: Path, action: str, scope: str,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        project = str(tmp_path / "project")
        _run(state, _receive(state, project, run_id="old-run"))
        surface = ghost_control_surface(state)
        request = _request(project, action=action, scope=scope)
        status, payload = surface.dispatch_action(request)
        assert status == 200, payload
        manifest = _manifest(payload)
        assert manifest.status == "completed" and len(manifest.stores) == 7
        assert surface.observations is not None
        saved = DeletionManifestStore(surface.observations.database)
        assert saved.get(manifest.operation_id) == manifest
        assert len(saved.events(manifest.operation_id)) == 8
        _run(state, _receive(state, project, run_id="fresh-run"))
        request["delete_operation_id"] = manifest.operation_id
        retried_status, retried = ghost_control_surface(state).dispatch_action(request)
        assert retried_status == 200, retried
        assert retried["results"] == payload["results"]
        assert _manifest(retried) == manifest
        assert [row.get("run_id") for row in surface.observations.read_all()] == ["fresh-run"]


def test_partial_surface_records_only_available_stores_and_preserves_disable(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path)
    assert inbox.set_control("admit_hints", False)
    status, payload = GhostControlSurface(inbox=inbox).dispatch_action(_request("", action="reset_all"))
    assert status == 200, payload
    manifest = _manifest(payload)
    assert tuple(item.store for item in manifest.stores) == ("inbox",)
    results = payload["results"]
    assert mapping_fields(results) and results["observations"] == "unavailable"
    assert not GhostInboxStore(tmp_path).control_enabled("admit_hints")


def test_pending_operation_reopens_without_repeating_committed_store(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    _seed(surface, project)
    assert surface.inbox is not None
    manifests = DeletionManifestStore(surface.inbox.records.database)
    pending = new_manifest(
        operation_id="pending", action="delete_scope", scope="project", project=project,
        requested_at="2026-11-17T00:00:00Z", retention_class="tombstone", stores=GHOST_STORE_NAMES,
    )
    manifests.start(pending)
    with manifests.transaction():
        removed = surface.inbox.delete_scope("project", project=project)
        manifests.append(store_event(pending, "inbox", "completed", detail=json.dumps(removed)))
    reopened = GhostControlSurface.from_state_home(tmp_path)
    with patch.object(reopened.inbox, "delete_scope", side_effect=AssertionError("completed inbox repeated")):
        status, payload = reopened.dispatch_action(_request(project, identity="pending"))
    assert status == 200, payload
    assert _manifest(payload).status == "completed"
    assert _manifest(payload).requested_at == pending.requested_at
    assert reopened.observations is not None and reopened.observations.read_all() == ()


@pytest.mark.parametrize("store_name", GHOST_STORE_NAMES)
def test_blocked_store_is_terminal_but_other_stores_finish_and_new_operation_recovers(
    tmp_path: Path, store_name: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    request = _request(str(tmp_path / "project"), identity="blocked")
    store = _stores(surface)[store_name]
    with patch.object(store, "delete_scope", return_value=False):
        status, payload = surface.dispatch_action(request)
    assert status == 500, payload
    blocked = _manifest(payload)
    assert blocked.status == "blocked" and blocked.pending_stores == ()
    assert all(item.status == ("blocked" if item.store == store_name else "completed") for item in blocked.stores)
    with patch.object(store, "delete_scope", side_effect=AssertionError("terminal store repeated")):
        retry_status, retry = surface.dispatch_action(request)
    assert retry_status == 500 and _manifest(retry) == blocked
    request["delete_operation_id"] = "explicit-recovery"
    recovered_status, recovered = surface.dispatch_action(request)
    assert recovered_status == 200 and _manifest(recovered).status == "completed"


@pytest.mark.parametrize("change", ["project", "scope", "action"])
def test_operation_cannot_be_reused_for_a_different_effective_scope_or_action(tmp_path: Path, change: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    request = _request(str(tmp_path / "project"), identity="scope-bound")
    assert surface.dispatch_action(request)[0] == 200
    _seed(surface, str(tmp_path / "fresh"), run_id="fresh")
    request[change] = {"project": str(tmp_path / "fresh"), "scope": "session", "action": "reset_all"}[change]
    status, payload = surface.dispatch_action(request)
    assert status == 400 and payload["ok"] is False
    assert surface.observations is not None and len(surface.observations.read_all()) == 1


def test_irrelevant_request_context_does_not_change_effective_user_scope(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    request = _request("project-one", scope="user", identity="user-delete")
    status, payload = surface.dispatch_action(request)
    assert status == 200
    request.update(project="project-two", session_id="another-session")
    retry_status, retry = surface.dispatch_action(request)
    assert retry_status == 200 and _manifest(retry) == _manifest(payload)


@pytest.mark.parametrize("identity", [None, True, [], "", "x" * 121])
def test_invalid_operation_identity_is_rejected_before_mutation(tmp_path: Path, identity: object) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    request = _request("project")
    request["delete_operation_id"] = identity
    with patch.object(surface.inbox, "delete_scope", side_effect=AssertionError("invalid request mutated")):
        assert surface.dispatch_action(request)[0] == 400


def test_corrupt_existing_operation_fails_closed_without_new_delete(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    request = _request("project", identity="corrupt")
    assert surface.dispatch_action(request)[0] == 200
    assert surface.inbox is not None
    with surface.inbox.records.database.connection() as connection:
        connection.execute("UPDATE ghost_deletion_manifests SET manifest='{}' WHERE operation_id='corrupt'")
    with patch.object(surface.inbox, "delete_scope", side_effect=AssertionError("corruption caused new delete")):
        status, payload = surface.dispatch_action(request)
    assert status == 500 and payload["ok"] is False


def test_mixed_database_surface_is_rejected_before_any_mutation(tmp_path: Path) -> None:
    inbox = GhostInboxStore(tmp_path / "one")
    surface = GhostControlSurface(inbox=inbox, observations=GhostObservationStore(tmp_path / "two"))
    with patch.object(inbox, "delete_scope", side_effect=AssertionError("mixed database mutated")):
        assert surface.dispatch_action(_request("project"))[0] == 500


def test_durable_result_longer_than_240_characters_is_exact_on_retry(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    expected: dict[str, object] = {"nodes": 0, "edges": 0, "warnings": ["warning" * 100]}
    request = _request("project", identity="long-result")
    with patch.object(surface.hebbian, "delete_scope", return_value=expected):
        status, payload = surface.dispatch_action(request)
    assert status == 200, payload
    results = payload["results"]
    assert mapping_fields(results) and results["hebbian"] == expected
    assert len(_manifest(payload).store("hebbian").detail) > 240
    retry_status, retry = GhostControlSurface.from_state_home(tmp_path).dispatch_action(request)
    assert retry_status == 200 and retry["results"] == results


def test_oversize_result_rolls_back_actual_store_mutation_and_ack(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    _seed(surface, project)
    assert surface.observations is not None
    original = surface.observations.delete_scope

    def oversize(scope: str, *, project: str = "", session_id: str = "") -> object:
        original(scope, project=project, session_id=session_id)
        return {"oversize": "x" * MAX_STORE_DETAIL_CHARS}

    with patch.object(surface.observations, "delete_scope", oversize):
        status, payload = surface.dispatch_action(_request(project))
    assert status == 500, payload
    assert _manifest(payload).store("observations").status == "blocked"
    assert len(surface.observations.read_all()) == 1


def test_concurrent_same_operation_only_invokes_observation_delete_once(tmp_path: Path) -> None:
    first, second = (GhostControlSurface.from_state_home(tmp_path) for _ in range(2))
    assert first.observations is not None and second.observations is not None
    original = first.observations.delete_scope
    mutated, release, attempted = Event(), Event(), Event()
    calls: list[str] = []

    def pause(scope: str, *, project: str = "", session_id: str = "") -> object:
        calls.append(scope)
        result = original(scope, project=project, session_id=session_id)
        mutated.set()
        assert release.wait(10)
        return result

    request = _request("project", identity="concurrent")

    def competing() -> tuple[int, dict[str, object]]:
        attempted.set()
        return second.dispatch_action(request)

    with patch.object(first.observations, "delete_scope", pause), ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(first.dispatch_action, request)
        try:
            assert mutated.wait(10)
            b = executor.submit(competing)
            assert attempted.wait(10)
            release.set()
            a_status, a_payload = a.result(timeout=15)
            b_status, b_payload = b.result(timeout=15)
        finally:
            release.set()
    assert a_status == b_status == 200
    assert _manifest(a_payload) == _manifest(b_payload)
    assert calls == ["project"]


def test_boolean_schema_is_not_an_integer_manifest_version(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    status, payload = surface.dispatch_action(_request("project"))
    assert status == 200
    invalid = _manifest(payload).to_payload()
    invalid["schema_version"] = True
    with pytest.raises(DeletionManifestError, match="schema"):
        DeletionManifest.from_payload(invalid)


def test_find_distinguishes_absence_from_corruption(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    manifests = DeletionManifestStore(database)
    assert manifests.find("absent") is None
    pending = new_manifest(
        operation_id="existing", action="reset_all", scope="all", requested_at="now",
        retention_class="tombstone", stores=("inbox",),
    )
    manifests.start(pending)
    assert manifests.find("existing") == pending
    with database.connection() as connection:
        connection.execute("DELETE FROM ghost_deletion_manifest_events WHERE operation_id='existing'")
    with pytest.raises(DeletionManifestError):
        manifests.find("existing")


def test_invalid_completed_result_is_rejected_before_any_pending_mutation(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    assert surface.inbox is not None
    manifests = DeletionManifestStore(surface.inbox.records.database)
    pending = new_manifest(
        operation_id="invalid-detail", action="delete_scope", scope="project", project=project,
        requested_at="now", retention_class="tombstone", stores=GHOST_STORE_NAMES,
    )
    manifests.start(pending)
    manifests.append(store_event(pending, "inbox", "completed", detail="{"))
    with patch.object(surface.hebbian, "delete_scope", side_effect=AssertionError("invalid result allowed mutation")):
        assert surface.dispatch_action(_request(project, identity="invalid-detail"))[0] == 500


@pytest.mark.parametrize("command", ["delete-scope", "reset"])
def test_real_cli_uses_default_manifest_without_an_enable_flag(tmp_path: Path, command: str) -> None:
    args = [sys.executable, "-m", "codey", "ghost", command, "--state-home", str(tmp_path), "--yes"]
    if command == "delete-scope":
        args.append("user")
    completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stderr
    payload: object = json.loads(completed.stdout)
    assert mapping_fields(payload)
    manifest = DeletionManifest.from_payload(payload["deletion_manifest"])
    assert manifest.status == "completed" and len(manifest.stores) == 7
    assert DeletionManifestStore(GhostSQLite(tmp_path)).get(manifest.operation_id) == manifest


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
def test_real_authenticated_http_route_defaults_to_manifest_and_unauthenticated_is_denied(
    tmp_path: Path, action: str,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state, CodeyHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        httpd.app_context = state
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with closing(HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)) as connection:
                body = json.dumps(_request("project", action=action))
                connection.request("POST", "/api/ghost/action", body=body, headers={"Content-Type": "application/json"})
                denied = connection.getresponse()
                denied.read()
                assert denied.status == 401
                bootstrap: dict[str, str] = {"token": httpd.operator_auth.issue_bootstrap()}
                connection.request(
                    "POST", "/api/operator_session",
                    body=json.dumps(bootstrap),
                    headers={"Content-Type": "application/json"},
                )
                exchange = connection.getresponse()
                exchange.read()
                assert exchange.status == 200
                cookie = exchange.getheader("Set-Cookie")
                assert cookie is not None
                connection.request(
                    "POST", "/api/ghost/action", body=body,
                    headers={"Content-Type": "application/json", "Cookie": cookie.split(";", 1)[0]},
                )
                response = connection.getresponse()
                payload: object = json.loads(response.read())
                assert response.status == 200, payload
                assert mapping_fields(payload)
                manifest = DeletionManifest.from_payload(payload["deletion_manifest"])
                assert manifest.status == "completed" and len(manifest.stores) == 7
                assert DeletionManifestStore(GhostSQLite(tmp_path)).get(manifest.operation_id) == manifest
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
