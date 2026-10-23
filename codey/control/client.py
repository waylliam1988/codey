"""Bounded local HTTP control client. Never imports the execution runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from codey.control.protocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, ControlError, fingerprint, validate_command
from codey.launcher.releases import activate, ensure_frontend, prepare, release_path, runtime_digest, snapshot
from codey.launcher.startup import startup_stage
from codey.storage.file_lock import acquire_lease
from codey.storage.local_store import DEFAULT_STATE_HOME, read_json_strict, write_json_atomic

if os.name == "nt" or TYPE_CHECKING:
    # CREATE_NO_WINDOW is a Windows-only subprocess constant and is absent
    # from Linux typeshed.
    _windows_subprocess: Any = subprocess


class ControlClient:
    def __init__(self, home: Path = DEFAULT_STATE_HOME, *, operator: bool = False) -> None:
        self.home = home.expanduser().resolve()
        self.operator = operator

    def discover(self) -> dict[str, Any]:
        info = read_json_strict(self.home / "service.json", max_bytes=8192)
        if info is None:
            raise ControlError("NOT_FOUND", "Codey service is not running; start codey serve or codey ui")
        url = urlparse(str(info.get("url") or ""))
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password:
            raise ControlError("FORBIDDEN", "Discovery must name an authenticated loopback service")
        if info.get("protocol_version") != PROTOCOL_VERSION:
            raise ControlError("PROTOCOL_MISMATCH", "Codey service protocol is incompatible")
        return info

    def _request(self, operation: str, params: dict[str, Any], *, lifecycle: bool = False,
                 timeout: float = 15) -> dict[str, Any]:
        info = self.discover()
        secret = read_json_strict(self.home / "control-credentials.json", max_bytes=8192)
        if secret is None:
            raise ControlError("UNAUTHORIZED", "Local control credentials are unavailable")
        data = json.dumps({"operation": operation, "params": params}, ensure_ascii=False, allow_nan=False).encode()
        request = urllib.request.Request(info["url"] + ("/api/lifecycle" if lifecycle else "/api/control"), data=data,
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret["operator" if self.operator else "agent"],
                                                  "X-Codey-Instance": info["instance_id"]})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read(MAX_FRAME_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_FRAME_BYTES + 1)
        except (OSError, TimeoutError) as exc:
            raise ControlError("OUTCOME_UNKNOWN", "Connection ended; query the original request identity before retrying",
                               details={"request_id": params.get("request_id"), "instance_id": info["instance_id"]}) from exc
        if len(raw) > MAX_FRAME_BYTES:
            raise ControlError("RESPONSE_TOO_LARGE", "Response exceeds the bounded client frame")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ControlError("OUTCOME_UNKNOWN", "Service response was incomplete") from exc
        if payload.get("ok") is not True:
            error = payload.get("error") or {}
            raise ControlError(error.get("code", "INTERNAL_ERROR"), error.get("message", "Service rejected the command"), details=error.get("details"))
        return dict(payload["result"])

    def call(self, operation: str, params: object = None) -> dict[str, Any]:
        args = validate_command(operation, {} if params is None else params)
        if operation == "get_upgrade":
            # Observation must survive the HTTP service being replaced.
            from codey.launcher.releases import read_upgrade

            return read_upgrade(self.home, args["upgrade_id"])
        timeout = 35 if operation == "wait_run" else 300 if operation == "prepare_release" else 60 if operation == "send_message" else 15
        # Queries are cheap to retry at the caller. No automatic write retry,
        # new request ID, or service replacement is hidden in this adapter.
        return self._request(operation, args, timeout=timeout)

    def lifecycle(self, operation: str) -> dict[str, Any]:
        return self._request(operation, {"expected_instance_id": self.discover()["instance_id"]}, lifecycle=True)


def _current_source() -> Path:
    source = Path(__file__).resolve().parents[2]
    return source


def _materialize_current_release(home: Path) -> dict[str, Any]:
    source = _current_source()
    with acquire_lease(home / "release-preparation"):
        if (source / "tools/publish_runtime.py").is_file() and (source / "pyproject.toml").is_file():
            # Development publication only; wheels already carry this artifact.
            from tools.publish_runtime import publish_runtime
            with startup_stage("frontend.check"):
                ensure_frontend(source)
            publish_runtime(source, source / "pyproject.toml")
        with startup_stage("release.snapshot"):
            return snapshot(home, source)


def _launch_release(client: ControlClient, home: Path, release: dict[str, Any], *, port: int,
                    allowed_roots: tuple[Path, ...]) -> ControlClient:
    args = [sys.executable, "-m", "codey", "serve", "--state-home", str(home), "--port", str(port),
            "--release-id", release["release_id"]]
    for root in allowed_roots or (Path.cwd(),):
        args.extend(["--allow-project", str(root.resolve())])
    log = home / "service-startup.log"
    if log.exists() and log.stat().st_size > MAX_FRAME_BYTES:
        log.replace(home / "service-startup.previous.log")
    with log.open("ab") as stream:
        child = subprocess.Popen(args, cwd=release["source"], stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=stream,
                                 creationflags=_windows_subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            health = client.call("get_health")
            if health.get("release_id") == release["release_id"] and health.get("status") == "ready":
                write_json_atomic(home / "active-release.json", release, mode=0o600)
                return client
        except ControlError:
            if child.poll() not in (None, 0):
                raise ControlError("STARTUP_FAILED", "Codey service failed; inspect service-startup.log") from None
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
    raise ControlError("STARTUP_FAILED", "Codey service did not become ready within the startup deadline")


def _activate_current_release(client: ControlClient, home: Path, current: dict[str, Any],
                              health: dict[str, Any], *, allowed_roots: tuple[Path, ...]) -> ControlClient:
    digest = current["candidate_digest"]
    # Recovery snapshots may have a different identity for identical content.
    # A healthy service already running that content requires no migration.
    if health.get("status") == "ready":
        try:
            running_source = release_path(home, str(health.get("release_id", ""))) / "source"
            identical = runtime_digest(running_source) == digest
        except (ControlError, OSError):
            identical = False
        if identical:
            write_json_atomic(home / "active-release.json", {
                **current, "release_id": health["release_id"], "source": str(running_source),
            }, mode=0o600)
            return client
    # Automatic transitions are owned by the current launcher process. The
    # service on the port may be an older release and must not be asked to
    # execute preparation code from its own snapshot.
    coordinator = SimpleNamespace(
        ctx=SimpleNamespace(state_home=home),
        instance_id=health["instance_id"],
        allowed_roots=allowed_roots,
    )
    # This is a new preparation attempt, not a replay of old database evidence.
    # Static checks have their own environment-bound cache; state preflight runs again.
    prepare_args = {"request_id": "auto-local-prepare-" + uuid.uuid4().hex,
                    "source": str(_current_source())}
    try:
        with startup_stage("release.prepare"):
            candidate = prepare(coordinator, prepare_args)
    except ControlError as exc:
        if exc.code != "OUTCOME_UNKNOWN":
            raise
        prepare_args["request_id"] = prepare_args["request_id"] + "-recovery-" + uuid.uuid4().hex
        candidate = prepare(coordinator, prepare_args)
    if candidate.get("candidate_digest") != digest:
        raise ControlError("REVISION_CONFLICT", "Current source changed while the release was being prepared")
    # Interrupted validation can produce a recovery release with the same
    # digest but a different identity. Reuse it on subsequent desktop opens.
    if health.get("release_id") == candidate["release_id"] and health.get("status") == "ready":
        return client
    activation_args = {
        "candidate_release": candidate["release_id"],
        "candidate_digest": candidate["candidate_digest"],
        "expected_instance_id": health["instance_id"],
    }
    # Each new preparation owns one activation attempt. Replaying that exact
    # activation request still observes its original journal; a user retry
    # after an aborted cutover gets a new attempt, not the old failed result.
    activation_args["request_id"] = "auto-activate-" + fingerprint({
        **activation_args, "preparation_request_id": prepare_args["request_id"],
    })[:32]
    activation = activate(coordinator, activation_args)
    upgrade_id = activation["upgrade_id"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            status = client.call("get_upgrade", {"upgrade_id": upgrade_id})
        except ControlError as exc:
            if exc.code == "NOT_FOUND":
                time.sleep(0.1)
                continue
            raise
        phase = status.get("phase")
        if phase in {"rolled_back", "recovery_required", "aborted"}:
            raise ControlError("UPGRADE_FAILED", "Codey could not activate the current release", details=status)
        if phase == "active":
            try:
                ready = client.call("get_health")
                if ready.get("release_id") == candidate["release_id"] and ready.get("status") == "ready":
                    return client
            except ControlError:
                pass
        time.sleep(0.1)
    raise ControlError("UPGRADE_FAILED", "Codey release activation did not settle", details={"upgrade_id": upgrade_id})


def ensure_service(home: Path, *, port: int = 0, allowed_roots: tuple[Path, ...] = ()) -> ControlClient:
    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    # Serialize the complete materialize/handshake/cutover sequence so two
    # desktop launches cannot prepare or activate competing candidates.
    with startup_stage("startup.lock"):
        lease = acquire_lease(home / "startup.lock", timeout_seconds=90)
    with lease:
        current = _materialize_current_release(home)
        client = ControlClient(home, operator=True)
        try:
            with startup_stage("service.health"):
                health = client.call("get_health")
        except ControlError as exc:
            if exc.code not in {"NOT_FOUND", "OUTCOME_UNKNOWN"}:
                raise
            with startup_stage("service.launch"):
                return _launch_release(client, home, current, port=port, allowed_roots=allowed_roots)
        if health.get("release_id") == current["release_id"]:
            if health.get("status") == "ready":
                write_json_atomic(home / "active-release.json", current, mode=0o600)
                return client
            raise ControlError("STARTUP_FAILED", "Codey service has the requested release but is not ready")
        with startup_stage("service.activate"):
            return _activate_current_release(client, home, current, health, allowed_roots=allowed_roots)
