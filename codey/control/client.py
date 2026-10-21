"""Bounded local HTTP control client. Never imports the execution runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from codey.control.protocol import MAX_FRAME_BYTES, PROTOCOL_VERSION, ControlError, validate_command
from codey.storage.local_store import DEFAULT_STATE_HOME, read_json_strict


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


def ensure_service(home: Path, *, port: int = 0, allowed_roots: tuple[Path, ...] = ()) -> ControlClient:
    client = ControlClient(home, operator=True)
    try:
        client.call("get_health")
        return client
    except ControlError as exc:
        if exc.code not in {"NOT_FOUND", "OUTCOME_UNKNOWN"}:
            raise
    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "codey", "serve", "--state-home", str(home), "--port", str(port)]
    source = Path(__file__).resolve().parents[2]
    active = read_json_strict(home / "active-release.json")
    if active:
        from codey.launcher.releases import release_path, runtime_digest

        source = release_path(home, str(active.get("release_id") or "")) / "source"
        if str(source) != active.get("source") or runtime_digest(source) != active.get("candidate_digest"):
            raise ControlError("REVISION_CONFLICT", "Committed runtime changed; inspect release evidence before restarting")
        args.extend(["--release-id", active["release_id"]])
    for root in allowed_roots or (Path.cwd(),):
        args.extend(["--allow-project", str(root.resolve())])
    log = home / "service-startup.log"
    if log.exists() and log.stat().st_size > MAX_FRAME_BYTES:
        log.replace(home / "service-startup.previous.log")
    with log.open("ab") as stream:
        child = subprocess.Popen(args, cwd=source, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=stream,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            client.call("get_health")
            return client
        except ControlError:
            if child.poll() not in (None, 0):
                raise ControlError("STARTUP_FAILED", "Codey service failed; inspect service-startup.log") from None
        time.sleep(0.1)
    raise ControlError("STARTUP_FAILED", "Codey service did not become ready within the startup deadline")
