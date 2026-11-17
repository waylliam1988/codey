"""Ordinary discovery and explicit recovery after a lost deletion response."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode

import pytest

from codey.app.context import AppContext
from codey.app.server import CodeyHTTPServer, Handler
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest, DeletionManifestError, new_manifest, store_event
from codey.ghost.deletion_manifest_coordinator import DeletionManifestCoordinator
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.payload_types import mapping_fields
from codey.ghost.sqlite import GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed


def _runtime_digest() -> str:
    digest = hashlib.sha256()
    for source in sorted(Path("codey").rglob("*.py")):
        digest.update(source.as_posix().encode())
        digest.update(source.read_bytes())
    return digest.hexdigest()


def _crash(home: Path, window: str, scope: str = "project") -> None:
    """Called in a fresh process; no response or operation ID reaches parent."""
    surface = GhostControlSurface.from_state_home(home)
    request: dict[str, object] = {
        "action": "reset_all" if scope == "all" else "delete_scope", "scope": scope,
        "project": str(home / "project"), "session_id": "session", "confirm": True,
    }
    if window == "started":
        def started(coordinator: DeletionManifestCoordinator, manifest: DeletionManifest) -> DeletionManifest:
            os._exit(73)

        with patch.object(DeletionManifestCoordinator, "run", started):
            surface.dispatch_action(request)
    elif window == "between_stores":
        assert surface.hebbian is not None

        def second_store(scope: str = "", *, project: str = "", session_id: str = "") -> object:
            os._exit(73)

        with patch.object(surface.hebbian, "reset_all" if scope == "all" else "delete_scope", second_store):
            surface.dispatch_action(request)
    elif window == "before_response":
        original_run = DeletionManifestCoordinator.run

        def completed(coordinator: DeletionManifestCoordinator, manifest: DeletionManifest) -> DeletionManifest:
            original_run(coordinator, manifest)
            os._exit(73)

        with patch.object(DeletionManifestCoordinator, "run", completed):
            surface.dispatch_action(request)
    else:
        raise ValueError(window)
    raise AssertionError("crash hook was not reached")


def _items(payload: object) -> list[dict[str, object]]:
    assert mapping_fields(payload)
    rows = payload["items"]
    assert isinstance(rows, list)
    values: list[object] = rows
    result: list[dict[str, object]] = []
    for value in values:
        assert mapping_fields(value)
        result.append(dict(value))
    return result


@pytest.mark.parametrize("window", ["started", "between_stores", "before_response"])
@pytest.mark.parametrize("scope", ["user", "project", "session", "all"])
def test_lost_response_is_discoverable_and_recoverable_without_database_queries(tmp_path: Path, window: str, scope: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    _seed(surface, project, scope=scope if scope != "all" else "user")
    before = _runtime_digest()
    script = "from pathlib import Path; import sys; from tests.test_ghost_manifest_discovery import _crash; _crash(Path(sys.argv[1]), sys.argv[2], sys.argv[3])"
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path), window, scope], capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stderr
    assert crashed.stdout == ""
    assert _runtime_digest() == before
    reopened = GhostControlSurface.from_state_home(tmp_path)
    exported = reopened.export_state()
    assert exported["ok"] is True
    rows = _items(exported["deletions"])
    assert len(rows) == 1
    operation = rows[0]
    assert operation["status"] == ("completed" if window == "before_response" else "pending")
    assert operation["project"] == (project if scope == "project" else "")
    assert operation["session_id"] == ("session" if scope == "session" else "")
    summary_rows = _items(reopened.summary(project=project, session_id="session")["deletions"])
    assert summary_rows == rows
    identity = operation["delete_operation_id"]
    assert isinstance(identity, str) and identity
    request: dict[str, object] = {
        "action": operation["action"], "scope": operation["scope"], "project": operation["project"],
        "session_id": operation["session_id"], "confirm": True, "delete_operation_id": identity,
    }
    if window == "before_response":
        _seed(reopened, project, run_id="fresh")
    status, response = reopened.dispatch_action(request)
    assert status == 200, response
    assert DeletionManifest.from_payload(response["deletion_manifest"]).status == "completed"
    assert reopened.observations is not None
    assert len(reopened.observations.read_all()) == (1 if window == "before_response" else 0)
    after_rows = _items(reopened.export_state()["deletions"])
    assert after_rows[0]["delete_operation_id"] == identity
    assert after_rows[0]["status"] == "completed"


def _start(home: Path, identity: str, *, scope: str = "user", project: str = "", session_id: str = "") -> DeletionManifest:
    return DeletionManifestStore(GhostSQLite(home)).start(new_manifest(
        operation_id=identity, action="reset_all" if scope == "all" else "delete_scope", scope=scope,
        project=project, session_id=session_id, requested_at="2026-11-17T00:00:00Z",
        retention_class="tombstone", stores=("inbox",),
    ))


def _identities(payload: object) -> list[str]:
    identities: list[str] = []
    for row in _items(payload):
        identity = row["delete_operation_id"]
        assert isinstance(identity, str)
        identities.append(identity)
    return identities


def test_bounded_scan_pages_are_ordered_exact_and_survive_reopen(tmp_path: Path) -> None:
    store = DeletionManifestStore(GhostSQLite(tmp_path))
    with store.transaction():
        for number in reversed(range(107)):
            _start(tmp_path, f"op-{number:03}")
    page = store.list_page(limit=100)
    assert len(page.items) == 100 and page.next_cursor == "op-099"
    second = DeletionManifestStore(GhostSQLite(tmp_path)).list_page(after=page.next_cursor, limit=100)
    assert len(second.items) == 7 and second.next_cursor == ""
    assert [item.operation_id for item in (*page.items, *second.items)] == [f"op-{number:03}" for number in range(107)]
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert len(_items(surface.export_state()["deletions"])) == 20
    assert len(_items(surface.summary()["deletions"])) == 20
    status, payload = surface.dispatch_action({"action": "list_deletions", "after": "op-099", "limit": 100})
    assert status == 200 and _identities(payload) == [f"op-{number:03}" for number in range(100, 107)]
    assert store.list_page(after="zzz").items == ()


@pytest.mark.parametrize("value", [True, False, 0, -1, 101, 1.5, "20", None, []])
def test_bad_page_limits_fail_before_any_mutation(tmp_path: Path, value: object) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    with patch.object(surface.inbox, "delete_scope", side_effect=AssertionError("read mutated")):
        status, payload = surface.dispatch_action({"action": "list_deletions", "limit": value})
    assert status == 400 and payload["ok"] is False


@pytest.mark.parametrize("value", [None, True, 23, [], "x" * 241])
def test_bad_cursors_are_rejected(tmp_path: Path, value: object) -> None:
    status, payload = GhostControlSurface.from_state_home(tmp_path).dispatch_action({"action": "list_deletions", "after": value})
    assert status == 400 and payload["ok"] is False


def test_scoped_summary_and_filtered_pagination_hide_other_scope_contents(tmp_path: Path) -> None:
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    _start(tmp_path, "a-other-project", scope="project", project=second)
    _start(tmp_path, "b-other-session", scope="session", session_id="other")
    _start(tmp_path, "c-current-project", scope="project", project=first)
    _start(tmp_path, "d-current-session", scope="session", session_id="here")
    _start(tmp_path, "e-user")
    _start(tmp_path, "f-reset", scope="all")
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _identities(surface.summary(project=first, session_id="here")["deletions"]) == ["c-current-project", "d-current-session", "e-user", "f-reset"]
    assert _identities(surface.summary()["deletions"]) == ["e-user", "f-reset"]
    # Filtering occurs after the bounded canonical scan: an empty page can have a cursor.
    status, empty = surface.dispatch_action({"action": "list_deletions", "scope": "project", "project": first, "limit": 2})
    assert status == 200 and _items(empty) == [] and empty["next_cursor"] == "b-other-session"
    status, found = surface.dispatch_action({"action": "list_deletions", "scope": "project", "project": first, "limit": 2, "after": empty["next_cursor"]})
    assert status == 200 and _identities(found) == ["c-current-project"]
    for scope in ("project", "session", "unknown"):
        assert surface.dispatch_action({"action": "list_deletions", "scope": scope})[0] == 400


@pytest.mark.parametrize("corruption", ["payload", "events", "status", "identity", "orphan"])
def test_corruption_is_reported_by_discovery_not_hidden_as_absence(tmp_path: Path, corruption: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    _start(tmp_path, "corrupt")
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        if corruption in {"identity", "orphan"}:
            connection.execute("PRAGMA foreign_keys=OFF")
        if corruption == "payload":
            connection.execute("UPDATE ghost_deletion_manifests SET manifest='{}'")
        elif corruption == "events":
            connection.execute("DELETE FROM ghost_deletion_manifest_events")
        elif corruption == "status":
            connection.execute("UPDATE ghost_deletion_manifests SET status='completed'")
        elif corruption == "identity":
            connection.execute("UPDATE ghost_deletion_manifests SET operation_id='wrong'")
            connection.execute("UPDATE ghost_deletion_manifest_events SET operation_id='wrong'")
        else:
            connection.execute("DELETE FROM ghost_deletion_manifests")
    with pytest.raises(DeletionManifestError):
        DeletionManifestStore(database).list_page()
    status, payload = surface.dispatch_action({"action": "list_deletions"})
    assert status == 500 and payload["ok"] is False
    exported = surface.export_state()
    assert exported["ok"] is False
    assert exported["deletions"] == {"error": "deletions_export_failed"}
    summary = surface.summary()
    assert summary["deletions"] == {"ok": False, "error": "deletion_read_failed"}
    health = summary["health"]
    assert mapping_fields(health) and health["status"] == "warning"


def test_discovery_preserves_blocked_records_controls_and_store_state(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    assert surface.inbox.set_control("admit_hints", False)
    assert surface.inbox.set_control("run_maintenance", False)
    manifest = _start(tmp_path, "blocked")
    store = DeletionManifestStore(surface.inbox.records.database)
    store.append(store_event(manifest, "inbox", "blocked", detail="sensitive exception body"))
    before = surface.inbox.export_state()
    with patch.object(surface.inbox, "delete_scope", side_effect=AssertionError("discovery retried blocked")):
        status, payload = surface.dispatch_action({"action": "list_deletions"})
        exported = surface.export_state()
        summary = surface.summary()
    assert status == 200 and _items(payload)[0]["status"] == "blocked"
    discovery_payloads: list[object] = [payload, exported["deletions"], summary["deletions"]]
    assert "sensitive exception body" not in json.dumps(discovery_payloads)
    assert _items(payload)[0]["blocked_stores"] == ("inbox",)
    assert surface.inbox.export_state() == before
    assert store.get("blocked").status == "blocked"


def test_real_cli_export_discovers_the_default_operation(tmp_path: Path) -> None:
    status, response = GhostControlSurface.from_state_home(tmp_path).dispatch_action({"action": "reset_all", "confirm": True})
    assert status == 200
    completed = subprocess.run([sys.executable, "-m", "codey", "ghost", "export", "--state-home", str(tmp_path)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stderr
    payload: object = json.loads(completed.stdout)
    assert mapping_fields(payload)
    assert _identities(payload["deletions"]) == [response["delete_operation_id"]]


def test_authenticated_http_discovers_pages_and_recovers_original_scope(tmp_path: Path) -> None:
    project = str(tmp_path / "project")
    _start(tmp_path, "pending", scope="project", project=project)
    with closing(AppContext(tmp_path, start_scheduler=False)) as state, CodeyHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        httpd.app_context = state
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with closing(HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)) as connection:
                body: dict[str, object] = {"action": "list_deletions", "limit": 1}
                connection.request("POST", "/api/ghost/action", body=json.dumps(body), headers={"Content-Type": "application/json"})
                denied = connection.getresponse()
                denied.read()
                assert denied.status == 401
                bootstrap: dict[str, str] = {"token": httpd.operator_auth.issue_bootstrap()}
                connection.request("POST", "/api/operator_session", body=json.dumps(bootstrap), headers={"Content-Type": "application/json"})
                exchange = connection.getresponse()
                exchange.read()
                assert exchange.status == 200
                cookie = exchange.getheader("Set-Cookie")
                assert cookie is not None
                headers = {"Content-Type": "application/json", "Cookie": cookie.split(";", 1)[0]}
                connection.request("POST", "/api/ghost/action", body=json.dumps(body), headers=headers)
                response = connection.getresponse()
                payload: object = json.loads(response.read())
                assert response.status == 200 and _identities(payload) == ["pending"]
                row = _items(payload)[0]
                recovery: dict[str, object] = {"action": row["action"], "scope": row["scope"], "project": row["project"], "delete_operation_id": row["delete_operation_id"], "confirm": True}
                connection.request("POST", "/api/ghost/action", body=json.dumps(recovery), headers=headers)
                recovered = connection.getresponse()
                result: object = json.loads(recovered.read())
                assert recovered.status == 200 and mapping_fields(result)
                assert DeletionManifest.from_payload(result["deletion_manifest"]).status == "completed"
                connection.request("GET", "/api/ghost/export", headers=headers)
                exported = connection.getresponse()
                export_payload: object = json.loads(exported.read())
                assert exported.status == 200 and mapping_fields(export_payload)
                assert _items(export_payload["deletions"])[0]["status"] == "completed"
                connection.request("GET", "/api/ghost/summary?" + urlencode({"project": project}), headers=headers)
                summary = connection.getresponse()
                summary_payload: object = json.loads(summary.read())
                assert summary.status == 200 and mapping_fields(summary_payload)
                assert _identities(summary_payload["deletions"]) == ["pending"]
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
