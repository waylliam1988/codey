"""Parent-side JSON-line Provider worker wrapper."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from codey import cancellation
from codey.adapter_overrides import AdapterOverride, record_failure, record_success
from codey.browser import DEFAULT_PORT
from codey.local_store import DEFAULT_STATE_HOME
from codey.provider_diagnostics import (
    FAILURE_RESPONSE_MISSING,
    ProviderActionError,
    ProviderFailure,
)


WORKER_TIMEOUT_GRACE = 5.0


@dataclass
class WorkerChatProvider:
    provider_id: str
    override: AdapterOverride
    port: int = DEFAULT_PORT + 100
    state_home: Path = DEFAULT_STATE_HOME

    def __post_init__(self) -> None:
        self.name = f"{self.provider_id} worker"
        self.last_failure: ProviderFailure | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._job = None
        self._responses: queue.Queue[dict] = queue.Queue()
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._cdp_port: int = 0
        self._target_id: str = ""
        self._start()

    @property
    def location(self) -> str:
        return f"worker:{self.provider_id}:{self.override.generation}"

    def new_chat(self, timeout: float | None = None) -> None:
        self._request("new_chat", {"timeout": timeout}, timeout)

    def send(self, text: str, timeout: float | None = None) -> str:
        try:
            result = self._request("send", {"text": text, "timeout": timeout}, timeout)
        except ProviderActionError as exc:
            record_failure(
                self.provider_id,
                self.override.generation,
                exc.failure.kind,
                state_home=self.state_home,
            )
            raise
        record_success(
            self.provider_id,
            self.override.generation,
            state_home=self.state_home,
        )
        return str(result or "")

    def close(self) -> None:
        try:
            self._request("close", {}, 2.0)
        except Exception:
            pass
        self._terminate()

    def _start(self) -> None:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.override.root) + (os.pathsep + existing if existing else "")
        env["CODEY_PROVIDER_WORKER_CHILD"] = "1"
        cmd = [
            sys.executable,
            "-B",
            "-m",
            "codey.provider_worker_child",
            "--provider",
            self.provider_id,
            "--port",
            str(self.port),
        ]
        group_args: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.override.root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group_args,
        )
        try:
            self._job = cancellation.attach_process_tree(self._proc)
        except Exception:
            self._proc = None
            raise
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                if payload.get("event") == "page":
                    self._record_worker_page(payload)
                    continue
                self._responses.put(payload)

    def _record_worker_page(self, payload: dict) -> None:
        try:
            self._cdp_port = max(0, int(payload.get("port") or 0))
        except (TypeError, ValueError):
            self._cdp_port = 0
        self._target_id = str(payload.get("target_id") or "")

    def _request(self, method: str, params: dict, timeout: float | None):
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("provider worker is not running")
        request_id = uuid.uuid4().hex
        payload = {"id": request_id, "method": method, "params": params}
        with self._lock:
            try:
                proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                proc.stdin.flush()
            except OSError as exc:
                self._terminate()
                raise RuntimeError("provider worker stdin is unavailable") from exc
            deadline = time.monotonic() + (timeout if timeout is not None else 300.0) + WORKER_TIMEOUT_GRACE
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate()
                    failure = ProviderFailure(
                        self.provider_id,
                        method,
                        "",
                        "",
                        "provider worker timed out",
                        "",
                        FAILURE_RESPONSE_MISSING,
                    )
                    self.last_failure = failure
                    raise ProviderActionError(failure)
                if proc.poll() is not None:
                    failure = ProviderFailure(
                        self.provider_id,
                        method,
                        "",
                        "",
                        "provider worker exited",
                        "",
                        FAILURE_RESPONSE_MISSING,
                    )
                    self.last_failure = failure
                    raise ProviderActionError(failure)
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty as exc:
                    self._terminate()
                    failure = ProviderFailure(
                        self.provider_id,
                        method,
                        "",
                        "",
                        "provider worker timed out",
                        "",
                        FAILURE_RESPONSE_MISSING,
                    )
                    self.last_failure = failure
                    raise ProviderActionError(failure) from exc
                if response.get("id") != request_id:
                    continue
                if response.get("ok") is True:
                    return response.get("result")
                failure = _failure_from_response(self.provider_id, method, response)
                self.last_failure = failure
                raise ProviderActionError(failure)

    def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        job = self._job
        self._job = None
        if proc is None:
            return
        try:
            self._close_worker_page()
            cancellation.terminate_process_tree(proc, job)
        finally:
            if job is not None:
                try:
                    job.close()
                except Exception:
                    pass

    def _close_worker_page(self) -> None:
        port = self._cdp_port
        target_id = self._target_id
        self._target_id = ""
        if not port or not target_id:
            return
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/json/close/{quote(target_id, safe='')}",
                timeout=2.0,
            ):
                pass
        except Exception:
            pass


def _failure_from_response(provider_id: str, method: str, response: dict) -> ProviderFailure:
    raw = response.get("failure")
    if isinstance(raw, dict):
        return ProviderFailure(
            str(raw.get("model") or provider_id),
            str(raw.get("action") or method),
            "",
            "",
            str(raw.get("message") or response.get("error") or "provider worker failed"),
            str(raw.get("time") or ""),
            str(raw.get("kind") or FAILURE_RESPONSE_MISSING),
            str(raw.get("stage") or ""),
        )
    return ProviderFailure(
        provider_id,
        method,
        "",
        "",
        str(response.get("error") or "provider worker failed"),
        "",
        FAILURE_RESPONSE_MISSING,
    )
