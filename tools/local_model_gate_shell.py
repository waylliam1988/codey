"""Shell approval through the real HTTP entry, worker, ledger and API provider.

The live gate approves only its exact harmless probe command. Tests replace
the model HTTP peer, never the provider or task lifecycle.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from tools.local_model_gate_attempts import (
    GateTarget,
    isolate_gate_secondary_models,
    record_api_generations,
    write_json,
)

SHELL_CASES = ("shell_approve", "shell_deny", "shell_failure")
SHELL_MARKER = "SHELL_GATE_RESULT"


def probe_command(case: str) -> str:
    if case not in SHELL_CASES:
        raise ValueError("unsupported shell gate case")
    code = f"print('{SHELL_MARKER}')"
    if case == "shell_failure":
        code = "import sys; " + code + "; sys.exit(7)"
    return f'python -c "{code}"'


def shell_prompt(case: str) -> str:
    return (f"Use the shell tool with path '.' to request exactly this command:\n{probe_command(case)}\n"
            "Wait for approval. After receiving its result, finish with done and explain the actual "
            "output and exit code, or the user's denial. Do not repeat the command or change files.")


class GateHTTPClient:
    def __init__(self, httpd):
        host, port = httpd.server_address
        self.origin = f"http://{host}:{port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.post("/api/operator_session", {"token": httpd.operator_auth.issue_bootstrap()})

    def request(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.origin + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Origin": self.origin, "Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def post(self, path: str, body: dict) -> dict:
        status, payload = self.request(path, body)
        if status != 200:
            raise AssertionError(f"{path}: HTTP {status}: {payload}")
        return payload

    def state(self) -> dict:
        status, payload = self.request("/api/state")
        if status != 200:
            raise AssertionError(f"state: HTTP {status}: {payload}")
        return payload


@contextmanager
def gate_application(target: GateTarget, directory: Path):
    """Isolate app/config stores while using production factories and worker."""
    from codey.app import server
    from codey.app.context import AppContext
    from codey.providers import local_config

    directory.mkdir(parents=True, exist_ok=True)
    state_root = directory / "state"
    events = []
    env = {"NATIVE_TOOLS": "1"}
    if target.provider_id == "local":
        env.update(LOCAL_OPENAI_BASE_URL=target.base_url, LOCAL_OPENAI_MODEL=target.model)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch.object(local_config, "DEFAULT_STATE_HOME", state_root))
        if target.provider_id == "local":
            local_config.save_local_config(local_config.LocalProviderConfig(
                base_url=target.base_url, model=target.model, api_protocol=target.api_protocol,
                native_tools_mode="on", connection_revision="isolated-shell-gate",
                context=local_config.LocalContextBudget(target.context_window_tokens,
                    target.context_reserve_tokens, target.context_keep_recent_tokens)))
        state = AppContext(state_root)
        stack.callback(state.close)
        # Record production events without modifying dispatch or provider state.
        state._on_event_emitted = lambda row: events.append(dict(row))
        preferences = state.providers.model_preferences
        snapshot = preferences.snapshot()
        sources = snapshot["sources"]
        sources[target.provider_id] = {"enabled": True, "models": [target.model]}
        preferences.save(sources, base_revision=snapshot["revision"])
        state.ghost_inbox.set_learning_enabled(False)
        stack.enter_context(patch.object(server, "STATE", state))
        stack.enter_context(isolate_gate_secondary_models(target))
        stack.enter_context(record_api_generations(directory / "provider.jsonl"))
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd, state, events
        finally:
            state.request_stop()
            deadline = time.monotonic() + 10
            while state.is_busy() and time.monotonic() < deadline:
                time.sleep(0.02)
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            write_json(directory / "events.json", {"events": events})
            if state.is_busy() or thread.is_alive():
                raise RuntimeError("shell gate did not release its worker/server")


def wait_for_run(client: GateHTTPClient, run_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    snapshot = {}
    while time.monotonic() < deadline:
        snapshot = client.state()
        event = snapshot.get("last_terminal_event") or {}
        if not snapshot.get("busy") and event.get("run_id") == run_id:
            return event
        time.sleep(0.02)
    raise AssertionError(f"run did not finish: {snapshot}")


def verify_shell_events(events: list[dict], source_run: str, case: str, original_window: dict) -> dict:
    terminals = [row for row in events if row.get("type") == "task_done"]
    if any(row.get("stop_reason") not in {"approval", "done"} for row in terminals):
        raise AssertionError(f"intermediate task failure: {terminals}")
    results = [row for row in events if row.get("type") == "shell_result" and row.get("run_id") == source_run]
    if len(results) != 1:
        raise AssertionError(f"expected exactly one shell result: {results}")
    result = results[0]
    approved = case != "shell_deny"
    expected_exit = 7 if case == "shell_failure" else 0
    if result.get("approved") is not approved:
        raise AssertionError("approval decision mismatch")
    if approved and (result.get("exit_code") != expected_exit or SHELL_MARKER not in result.get("output", "")):
        raise AssertionError(f"execution output mismatch: {result}")
    if not approved and (result.get("exit_code") is not None or SHELL_MARKER in result.get("output", "")):
        raise AssertionError("denied command was reported as executed")
    finals = [row for row in terminals if row.get("stop_reason") == "done" and row.get("run_id") != source_run]
    if len(finals) != 1 or events.index(result) >= events.index(finals[0]):
        raise AssertionError("missing or out-of-order continuation completion")
    return {"source_run_id": source_run, "continuation_run_id": finals[0]["run_id"],
            "window_ref": original_window, "shell_results": 1,
            "stop_reason": finals[0]["stop_reason"], "summary": finals[0].get("summary", "")}


def run_shell_case(case: str, target: GateTarget, directory: Path) -> dict:
    if target.protocol != "native":
        raise ValueError("shell continuation gate requires native tools")
    started = time.monotonic()
    with gate_application(target, directory) as (httpd, state, events):
        client = GateHTTPClient(httpd)
        selection = {"model": target.model}
        if target.provider_id == "local":
            selection["base_url"] = target.base_url
        submitted = client.post("/api/run", {
            "session_id": "gate-shell", "input": {"kind": "start_task", "text": shell_prompt(case)},
            "provider": target.provider_id, "model_selection": selection, "intent": "chat", "max_turns": 6})
        source_run = submitted["run_id"]
        terminal = wait_for_run(client, source_run, target.request_timeout)
        if terminal.get("stop_reason") != "approval":
            raise AssertionError(f"shell request did not pause for approval: {terminal}")
        pending = state.pending_shell_approvals()
        if len(pending) != 1:
            raise AssertionError("expected one pending approval")
        approval_id, record = next(iter(pending.items()))
        if record.get("command") != probe_command(case):
            raise AssertionError("model requested a different command; gate will not approve it")
        original_window = record.get("window_ref")
        if not isinstance(original_window, dict) or not record.get("call_id"):
            raise AssertionError("approval lacks native window/call identity")
        approved = client.post("/api/shell_approval", {"id": approval_id, "approved": case != "shell_deny"})
        if approved.get("continued") is not True:
            raise AssertionError(f"approval did not submit continuation: {approved}")
        deadline = time.monotonic() + target.request_timeout
        while time.monotonic() < deadline:
            snapshot = client.state()
            final = snapshot.get("last_terminal_event") or {}
            if not snapshot.get("busy") and final.get("run_id") != source_run:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("approval continuation did not finish")
        proof = verify_shell_events(events, source_run, case, original_window)
        # Confirm window identity from the actual persisted protocol history.
        from codey.providers.context_ledger import ContextLedger
        from codey.runtime.core.protocol_window import ProtocolWindowRef

        ref = ProtocolWindowRef.from_payload(original_window)
        ledger = ContextLedger.for_protocol_session(directory / "state", ref.session_id, ref)
        if ledger.window_ref != ref:
            raise AssertionError("approval continuation changed the protocol window")
        call_key = "call_id" if target.api_protocol == "openai-responses" else "tool_call_id"
        result_items = [item for item in ledger.events() if item.get(call_key) == record["call_id"]
                        and (item.get("type") == "function_call_output" or item.get("role") == "tool")]
        if len(result_items) != 1:
            raise AssertionError("original shell result was not committed exactly once")
        status, _ = client.request("/api/shell_approval", {"id": approval_id, "approved": case != "shell_deny"})
        if status != 404:
            raise AssertionError("approval could be consumed a second time")
        return {"case": case, "ok": True, "seconds": round(time.monotonic() - started, 2),
                "scope": "approval_continuation", "failure_stage": "", **proof}
