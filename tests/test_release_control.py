"""Real-process release transitions; no provider/model calls are required."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codey.control.client import ControlClient
from codey.control.protocol import ControlError
from codey.launcher.releases import read_upgrade


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (ControlError, OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("Transition did not settle before the test deadline")


@pytest.fixture
def running_service(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    log = (home / "service-test.log").open("wb")
    process = subprocess.Popen([sys.executable, "-m", "codey", "serve", "--state-home", str(home)],
                               stdout=log, stderr=log,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    client = ControlClient(home, operator=True)
    wait_for(lambda: client.call("get_health"))
    yield home, client
    try:
        client.lifecycle("drain")
        client.lifecycle("shutdown")
    except ControlError:
        pass
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=10)
    log.close()


def test_verification_mode_blocks_all_admissions(tmp_path):
    from codey.app.service import LocalService

    runtime = LocalService(tmp_path, verify_only=True).start()
    try:
        client = ControlClient(tmp_path, operator=True)
        assert client.call("get_health")["status"] == "verifying"
        config = client.call("create_conversation", {"request_id": "one"})
        with pytest.raises(ControlError) as error:
            client.call("send_message", {"conversation_id": config["conversation_id"], "request_id": "two",
                                         "control_generation": 1, "text": "This must never execute"})
        assert error.value.code == "SERVER_DRAINING"
        assert not runtime.ctx.durable.tasks()
    finally:
        runtime.close()


def test_browser_session_survives_service_replacement_but_bootstrap_does_not(tmp_path):
    from codey.app.service import LocalService

    first = LocalService(tmp_path, verify_only=True).start()
    port = first.port
    bootstrap = first.httpd.operator_auth.issue_bootstrap()
    cookie = first.httpd.operator_auth.exchange(bootstrap)
    unused_bootstrap = first.httpd.operator_auth.issue_bootstrap()
    first.close()
    successor = LocalService(tmp_path, port=port, verify_only=True).start()
    try:
        assert successor.httpd.operator_auth.authenticated(cookie)
        assert successor.httpd.operator_auth.exchange(unused_bootstrap) is None
        assert successor.httpd.operator_auth._session_token != successor._secrets["operator"]
    finally:
        successor.close()


def test_real_release_restarts_and_recovers_visible_history(running_service):
    home, client = running_service
    initial = client.call("get_health")
    original_url = client.discover()["url"]
    config = client.call("create_conversation", {"request_id": "visible-before-upgrade"})
    candidate = client.call("prepare_release", {"request_id": "prepare-1", "source": str(Path.cwd())})
    assert candidate["status"] == "validated"
    request = {"request_id": "activate-1", "candidate_release": candidate["release_id"],
               "candidate_digest": candidate["candidate_digest"], "expected_instance_id": initial["instance_id"]}
    admitted = client.call("activate_release", request)
    result = wait_for(lambda: (value if (value := read_upgrade(home, admitted["upgrade_id"]))["phase"] in {"active", "rolled_back", "recovery_required"} else None), 60)
    assert result["phase"] == "active", json.dumps(result, ensure_ascii=False)
    health = client.call("get_health")
    assert health["instance_id"] != initial["instance_id"]
    assert client.discover()["url"] == original_url
    assert health["database_id"] == initial["database_id"]
    assert health["release_id"] == candidate["release_id"]
    assert client.call("get_control", {"conversation_id": config["conversation_id"]}) == config
    assert client.call("activate_release", request)["upgrade_id"] == admitted["upgrade_id"]


def test_failed_candidate_returns_to_an_immutable_previous_release(running_service):
    home, client = running_service
    initial = client.call("get_health")
    candidate = client.call("prepare_release", {"request_id": "prepare-failure", "source": str(Path.cwd())})
    # Fault injection happens AFTER prepare; activation must reject corruption
    # before draining the healthy instance.
    manifest = json.loads((home / "releases" / candidate["release_id"] / "manifest.json").read_text())
    source = Path(manifest["source"])
    (source / "codey" / "__main__.py").write_text("raise RuntimeError('candidate corrupt')\n")
    with pytest.raises(ControlError) as error:
        client.call("activate_release", {"request_id": "bad", "candidate_release": candidate["release_id"],
                                         "candidate_digest": candidate["candidate_digest"], "expected_instance_id": initial["instance_id"]})
    assert error.value.code == "REVISION_CONFLICT"
    assert client.call("get_health")["instance_id"] == initial["instance_id"]


def test_runner_rolls_back_startup_failure_without_restoring_database(running_service, monkeypatch):
    from codey.launcher import runner

    home, client = running_service
    initial = client.call("get_health")
    baseline = json.loads((home / "active-release.json").read_text())
    # Exercise the coordinator itself with a runtime that cannot start, rather
    # than bypassing activation's digest guard in production.
    journal = {"upgrade_id": "upgrade_" + "f" * 32, "phase": "accepted", "events": [], "attempts": 0,
               "old_instance_id": initial["instance_id"], "previous": baseline,
               "candidate": {**baseline, "release_id": "broken", "source": str(home / "missing")},
               "deadline": time.time() + 10, "python": sys.executable, "allowed_roots": [],
               "admission_may_have_opened": False}
    runner.coordinate(home, journal)
    assert journal["phase"] == "rolled_back", journal
    assert client.call("get_health")["database_id"] == initial["database_id"]
    assert client.call("get_health")["instance_id"] != initial["instance_id"]


def test_failed_validation_request_is_not_executed_twice(running_service):
    home, client = running_service
    marker = home / "validation-count.txt"
    script = f"from pathlib import Path; p=Path({str(marker)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); raise SystemExit(1)"
    args = {"request_id": "fail-once", "source": str(Path.cwd()), "verification": [[sys.executable, "-c", script]]}
    for _ in range(2):
        with pytest.raises(ControlError):
            client.call("prepare_release", args)
    assert marker.read_text() == "1"


def test_validation_cannot_edit_the_immutable_rollback_runtime(running_service):
    home, client = running_service
    active = json.loads((home / "active-release.json").read_text())
    target = Path(active["source"]) / "codey" / "__init__.py"
    before = target.read_bytes()
    args = {"request_id": "editing-validator", "source": str(Path.cwd()), "verification": [[sys.executable, "-c",
            "from pathlib import Path; p=Path('codey/__init__.py'); p.write_text(p.read_text()+'\\n# validator touched runtime\\n')"]]}
    with pytest.raises(ControlError):
        client.call("prepare_release", args)
    assert target.read_bytes() == before
