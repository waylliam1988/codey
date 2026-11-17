"""Normal headless/App HTTP crash recovery smoke with isolated durable state.

Run --self-test for the frozen scope/crash/revocation matrix. The child uses
real Local configuration, ApiProvider, SQLite and the normal App scheduler.
The loopback server supplies deterministic SSE replies; no model is called.
Raw requests, child logs and SQLite files stay in the chosen local state root.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.model import (
    ArtifactRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    SubmissionRecord,
    TaskCheckpoint,
)
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.payload_types import mapping_fields
from codey.providers import local_config
from codey.providers.catalog import API_CONNECTIONS, WEB_PROVIDER_LABELS
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.model_preferences import ModelPreferences
from codey.runs.details import load_run_details
from codey.runs.ledger import RunLedgerStore
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.core.operation_state import RuntimeOperationStore, operation_id_for_run, operation_progress_text
from codey.runtime.write.drive import peek_next_action
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object

MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
SESSION = "app-crash-session"
RUN = "app-crash-run"
SOURCE_RUN = "app-crash-source"
FRESH_RUN = "app-crash-fresh"
SOURCE_TASK = "Technical explanations are written in Chinese."
CRASH_TASK = "Explain eventual consistency for the crash recovery probe."
FRESH_TASK = "Explain a cache miss for the independent fresh task."
LANGUAGE_LINE = "- Prefer: reply language = Chinese."
SCOPES = ("user", "project", "session")
BOUNDARIES = ("request_ready", "intent_admitted", "wire_received", "response_saved")
STAGES = ("seed", "crash", "recover", "fresh")
TIMEOUT = 45.0


class FixtureError(RuntimeError):
    """The required real boundary was not reached; this cell is unscored."""


@dataclass(frozen=True)
class Case:
    root: Path
    scope: str
    boundary: str
    revoked: bool

    @property
    def home(self) -> Path:
        return self.root / "state"

    @property
    def project(self) -> Path:
        return self.root / "project"


@dataclass(frozen=True)
class CapturedRequest:
    raw: bytes
    payload: JsonObject


@dataclass
class Capture:
    endpoint: str = ""
    requests: list[CapturedRequest] = field(default_factory=list)
    received: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)

    def rows(self) -> tuple[CapturedRequest, ...]:
        with self.lock:
            return tuple(self.requests)


def _sse_reply(index: int) -> bytes:
    empty: JsonObject = {}
    frames: list[JsonObject] = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": f"probe-done-{index}", "type": "function", "function": {
                "name": "done", "arguments": '{"summary":"deterministic reply"}',
            },
        }]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": empty, "finish_reason": "tool_calls"}]},
    ]
    return b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + b"data: [DONE]\n\n"


@contextmanager
def _server(boundary: str) -> Generator[Capture, None, None]:
    capture = Capture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            payload: JsonObject = {"data": [{"id": MODEL}]} if self.path == "/v1/models" else {}
            self._reply(json.dumps(payload).encode(), "application/json")

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = parse_json_object(raw.decode("utf-8"))
            with capture.lock:
                capture.requests.append(CapturedRequest(raw, payload))
                index = len(capture.requests)
            if boundary == "wire_received" and CRASH_TASK in raw.decode("utf-8") and not capture.received.is_set():
                capture.received.set()
                if not capture.release.wait(timeout=TIMEOUT):
                    return
            try:
                self._reply(_sse_reply(index), "text/event-stream")
            except (BrokenPipeError, ConnectionResetError):
                # The wire_received cell deliberately kills the HTTP client.
                return

        def _reply(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        capture.endpoint = f"http://127.0.0.1:{server.server_port}/v1"

        def serve() -> None:
            server.serve_forever(poll_interval=0.02)

        worker = Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield capture
        finally:
            capture.release.set()
            server.shutdown()
            worker.join(timeout=5)
            if worker.is_alive():
                raise FixtureError("loopback server did not stop")


def _headless(case: Case, task: str, run_id: str, output: Path) -> None:
    rows: list[dict[str, object]] = []
    result = run_headless(
        HeadlessRequest(project=case.project, task=task, intent="chat", provider_id="local",
                        max_turns=4, session_id=SESSION, run_id=run_id, state_home=case.home),
        emit_jsonl=rows.append,
    )
    outcome: dict[str, object] = {"stop_reason": result.stop_reason, "exit_code": result.exit_code,
                                  "run_id": result.run_id, "events": rows}
    output.write_text(json.dumps(outcome), encoding="utf-8")


def _seed(case: Case) -> None:
    outcome = case.root / "seed-outcome.json"
    _headless(case, SOURCE_TASK, SOURCE_RUN, outcome)
    result = parse_json_object(outcome.read_text(encoding="utf-8"))
    if result["stop_reason"] != "done":
        raise FixtureError("normal source task did not complete")
    surface = GhostControlSurface.from_state_home(case.home)
    status, proposed = surface.dispatch_action({
        "action": "propose_preference", "id": SOURCE_RUN, "session_id": SESSION,
        "project": str(case.project), "scope": case.scope, "conflict_key": "language", "value_key": "chinese",
        "evidence_quote": SOURCE_TASK,
    })
    if status != 200:
        raise FixtureError(f"normal source proposal failed: {proposed}")
    candidate = proposed["candidate"]
    if not mapping_fields(candidate) or not isinstance(candidate.get("id"), str):
        raise FixtureError("normal proposal has no candidate identity")
    status, accepted = surface.dispatch_action({
        "action": "accept_candidate", "id": candidate["id"], "session_id": SESSION, "project": str(case.project),
    })
    if status != 200 or accepted.get("ok") is not True:
        raise FixtureError(f"normal candidate acceptance failed: {accepted}")


def _at_boundary(mutation: DurableMutation, boundary: str) -> bool:
    task = mutation.task
    if task is None or task.task_id != RUN:
        return False
    operation = operation_from_checkpoint(task, SESSION)
    if operation is None:
        return False
    if boundary == "request_ready":
        return operation.leaf == "provider_request_ready" and any(
            artifact.metadata.get("kind") == "provider_request" for artifact in mutation.artifacts)
    if boundary == "intent_admitted":
        return operation.leaf == "provider_effect_pending" and bool(mutation.effects or mutation.effect)
    return boundary == "response_saved" and operation.leaf == "provider_response_ready" and any(
        artifact.metadata.get("kind") == "provider_response" for artifact in mutation.artifacts)


def _crash(case: Case) -> None:
    commit = SQLiteDurableStore.commit

    def committed(store: SQLiteDurableStore, mutation: DurableMutation) -> CommitResult:
        result = commit(store, mutation)
        if _at_boundary(mutation, case.boundary):
            marker: dict[str, str] = {"boundary": case.boundary, "run_id": RUN}
            (case.root / "crash-marker.json").write_text(json.dumps(marker),
                                                       encoding="utf-8")
            os._exit(73)
        return result

    with patch.object(SQLiteDurableStore, "commit", committed):
        _headless(case, CRASH_TASK, RUN, case.root / "crash-outcome.json")
    raise FixtureError("normal task exited without reaching the specified hard-exit boundary")


def _recover(case: Case) -> None:
    state = AppContext(case.home)
    try:
        deadline = time.monotonic() + TIMEOUT - 5
        while time.monotonic() < deadline:
            task = state.durable.task(RUN)
            if task.phase in {"completed", "failed", "cancelled"} and not state.is_busy():
                break
            time.sleep(0.02)
        else:
            raise FixtureError("normal App scheduler did not reach an observable task conclusion")
        if not state.wait_for_ghost_sleep(timeout=5):
            raise FixtureError("normal App Ghost maintenance did not finish")
    finally:
        if not state.close():
            raise FixtureError("normal App did not close its owners")


def _child(case: Case, stage: str, endpoint: str) -> None:
    case.project.mkdir(parents=True, exist_ok=True)
    with patch.object(local_config, "DEFAULT_STATE_HOME", case.home):
        if stage == "seed":
            local_config.save_local_config(LocalProviderConfig(
                base_url=endpoint, model=MODEL, native_tools_mode="on", connection_revision="app-crash-fixture-v1",
                context=LocalContextBudget(262144, 32768, 32000, source="preset"),
            ))
            sources: dict[str, object] = {
                "websites": {"enabled": True, "models": list(WEB_PROVIDER_LABELS)},
                **{key: {"enabled": key == "local", "models": [MODEL] if key == "local" else []}
                   for key in API_CONNECTIONS},
            }
            preferences = ModelPreferences(case.home)
            saved: object = preferences.save(sources, base_revision=0)
            if not mapping_fields(saved) or not preferences.allows("local", MODEL):
                raise FixtureError("normal model allowlist setup did not enable the selected model")
            _seed(case)
        elif stage == "crash":
            _crash(case)
        elif stage == "recover":
            _recover(case)
        elif stage == "fresh":
            _headless(case, FRESH_TASK, FRESH_RUN, case.root / "fresh-outcome.json")
        else:
            raise ValueError("unknown child stage")


def _command(case: Case, stage: str, endpoint: str) -> list[str]:
    return [sys.executable, "-B", str(Path(__file__).resolve()), "--child", stage, "--state-root", str(case.root),
            "--scope", case.scope, "--boundary", case.boundary, "--endpoint", endpoint]


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if not key.startswith("LOCAL_OPENAI_") and key != "NATIVE_TOOLS"}


def _run_phase(case: Case, stage: str, endpoint: str) -> None:
    result = subprocess.run(_command(case, stage, endpoint), capture_output=True, text=True,
                            encoding="utf-8", env=_environment(), timeout=TIMEOUT)
    (case.root / f"{stage}-{time.monotonic_ns()}.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise FixtureError(f"{stage} child exit={result.returncode}: {result.stderr[-2000:]}")


def _kill_at_boundary(case: Case, capture: Capture) -> None:
    output = case.root / "crash-child.txt"
    with output.open("w", encoding="utf-8") as stream:
        child = subprocess.Popen(_command(case, "crash", capture.endpoint), stdout=stream, stderr=stream,
                                 env=_environment())
        try:
            if case.boundary == "wire_received":
                deadline = time.monotonic() + TIMEOUT
                while not capture.received.wait(timeout=0.02):
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise FixtureError("child did not reach real server request reception")
                if child.poll() is not None:
                    raise FixtureError("wire child stopped before parent hard kill")
                child.kill()
                child.wait(timeout=10)
            else:
                if child.wait(timeout=TIMEOUT) != 73 or not (case.root / "crash-marker.json").exists():
                    raise FixtureError(f"child did not commit requested boundary; inspect {output}")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            capture.release.set()


def _contains_path(value: object, path: str) -> bool:
    if isinstance(value, str):
        return path.replace("\\", "/") in value.replace("\\", "/")
    if mapping_fields(value):
        return any(_contains_path(item, path) for item in value.values())
    if isinstance(value, (tuple, list)):
        items: Sequence[object] = value
        return any(_contains_path(item, path) for item in items)
    return False


def _sent_state(effect: EffectRecord) -> str:
    if effect.settlement is None:
        return "unsettled"
    raw = effect.settlement.get("settlement", effect.settlement)
    if not mapping_fields(raw):
        raise FixtureError("effect settlement is not a valid object")
    state = raw.get("sent_state")
    return state if isinstance(state, str) else "no_sent_state"


@dataclass(frozen=True)
class Snapshot:
    task: TaskCheckpoint
    operation: RuntimeOperationState
    submission: SubmissionRecord
    requests: tuple[tuple[str, str], ...]
    responses: tuple[tuple[str, str], ...]
    model_requests: tuple[tuple[str, str], ...]
    model_responses: tuple[tuple[str, str], ...]
    effects: tuple[EffectRecord, ...]
    writer_entered: bool
    unique_identity: bool
    progress_matches: bool
    path_safe: bool
    next_action: str
    prepared_hint: bool

    def completed(self) -> bool:
        terminal = self.operation.terminal
        return (self.task.phase == "completed" and terminal is not None and terminal.stop_reason == "done"
                and self.submission.status == "done")

    def no_false_completion(self) -> bool:
        terminal = self.operation.terminal
        return (self.task.phase != "completed" and self.submission.status != "done"
                and (terminal is None or terminal.stop_reason != "done"))

    def metrics(self) -> dict[str, object]:
        return {"task_phase": self.task.phase, "leaf": self.operation.leaf,
                "stop_reason": self.operation.terminal.stop_reason if self.operation.terminal is not None else "",
                "submission_status": self.submission.status, "next_action": self.next_action,
                "request_refs": self.requests, "response_refs": self.responses,
                "model_request_refs": self.model_requests, "model_response_refs": self.model_responses,
                "effect_states": [[effect.effect_id, effect.status, _sent_state(effect)] for effect in self.effects],
                "writer_entered": self.writer_entered, "unique_identity": self.unique_identity,
                "progress_matches": self.progress_matches, "path_safe": self.path_safe,
                "prepared_hint": self.prepared_hint}


def _artifact_refs(artifacts: tuple[ArtifactRecord, ...], kind: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((artifact.handle, artifact.sha256) for artifact in artifacts if artifact.metadata.get("kind") == kind))


def _prepared_hint(case: Case, artifacts: tuple[ArtifactRecord, ...]) -> bool:
    requests = tuple(artifact for artifact in artifacts if artifact.metadata.get("kind") == "provider_request")
    if len(requests) != 1:
        raise FixtureError("expected one original model request artifact")
    reference = requests[0].metadata.get("ref")
    if not mapping_fields(reference):
        raise FixtureError("original request has no managed reference")
    envelope = decode_json_object(ManagedOutputStore(case.home).read_json_artifact(dict(reference)))
    payload = decode_json_object(envelope["payload"])
    context = decode_json_object(payload["context"])
    text = context["text"]
    if not isinstance(text, str):
        raise FixtureError("original request context text is invalid")
    return LANGUAGE_LINE in text


def _snapshot(case: Case, run_id: str = RUN) -> Snapshot:
    with closing(DurableSession(SQLiteDurableStore(case.home / "durable.sqlite3"))) as durable:
        task = durable.task(run_id)
        operation = operation_from_checkpoint(task, SESSION)
        if operation is None:
            raise FixtureError("normal task has no committed operation")
        submission = durable.submission(task.submission_id)
        artifacts = durable.artifacts_for_task(run_id)
        effects = durable.effects(run_id)
        # Native terminal acknowledgements commit protocol history without HTTP.
        acknowledgements = tuple(effect for effect in effects
                                 if effect.intent.get("display_ref") == "acknowledge_tool_results_bound")
        acknowledgement_refs = tuple(effect.intent.get("request_ref") for effect in acknowledgements)
        acknowledgement_ids = tuple(effect.effect_id for effect in acknowledgements)
        model_artifacts = tuple(artifact for artifact in artifacts
                                if artifact.handle not in acknowledgement_refs
                                and artifact.owner_effect_id not in acknowledgement_ids)
        timeline = durable.task_timeline(run_id)
        writer_entered = any(
            (state := operation_from_checkpoint(transition.task, SESSION)) is not None and state.leaf == "writer_running"
            for transition in timeline)
        details = load_run_details(run_ledgers=RunLedgerStore(case.home), run_traces=None,
                                   runtime_operations=RuntimeOperationStore(durable=durable), session_id=SESSION, run_id=run_id)
        progress = tuple(row.value for row in details.rows if row.label == "Progress")
        expected_progress = operation_progress_text(operation)
        return Snapshot(
            task, operation, submission, _artifact_refs(artifacts, "provider_request"),
            _artifact_refs(artifacts, "provider_response"), _artifact_refs(model_artifacts, "provider_request"),
            _artifact_refs(model_artifacts, "provider_response"), effects, writer_entered,
            operation.operation_id == operation_id_for_run(run_id)
            and sum(row.submission_id == task.submission_id for row in durable.tasks()) == 1,
            progress == ((expected_progress,) if expected_progress else ()),
            not _contains_path(operation.to_payload(), str(case.project)),
            peek_next_action(durable, session_id=SESSION, run_id=run_id).kind,
            _prepared_hint(case, model_artifacts),
        )


def _has_hint(request: CapturedRequest) -> bool:
    return LANGUAGE_LINE in request.raw.decode("utf-8")


def run_cell(root: Path, scope: str, boundary: str, revoked: bool) -> dict[str, object]:
    if scope not in SCOPES or boundary not in BOUNDARIES:
        raise ValueError("unknown cell scope or boundary")
    case = Case(root.resolve(), scope, boundary, revoked)
    case.root.mkdir(parents=True, exist_ok=True)
    with _server(boundary) as capture:
        _run_phase(case, "seed", capture.endpoint)
        seed_count = len(capture.rows())
        if seed_count != 1:
            raise FixtureError("normal source task did not make exactly one real HTTP request")
        if not ModelPreferences(case.home).allows("local", MODEL):
            raise FixtureError("the normal App selected model is disabled")
        _kill_at_boundary(case, capture)
        before = _snapshot(case)
        initial = capture.rows()[seed_count:]
        expected_leaf = {"request_ready": "provider_request_ready", "intent_admitted": "provider_effect_pending",
                         "wire_received": "provider_effect_pending", "response_saved": "provider_response_ready"}[boundary]
        expected_initial = int(boundary in {"wire_received", "response_saved"})
        if (before.operation.leaf != expected_leaf or not before.writer_entered or len(before.requests) != 1
                or not before.prepared_hint or len(initial) != expected_initial
                or any(not _has_hint(request) for request in initial)):
            raise FixtureError("the accepted source, writer or specified crash boundary is invalid")
        if revoked:
            surface = GhostControlSurface.from_state_home(case.home)
            if surface.inbox is None or not surface.inbox.set_control(
                "admit_hints", False, scope=scope, project=str(case.project), session_id=SESSION,
            ):
                raise FixtureError("scoped post-crash revocation did not commit")
        _run_phase(case, "recover", capture.endpoint)
        first = _snapshot(case)
        first_count = len(capture.rows()) - seed_count
        _run_phase(case, "recover", capture.endpoint)
        second = _snapshot(case)
        second_count = len(capture.rows()) - seed_count
        _run_phase(case, "fresh", capture.endpoint)
        fresh = _snapshot(case, FRESH_RUN)
        fresh_requests = capture.rows()[seed_count + second_count:]
        final_original = _snapshot(case)
        all_rows = capture.rows()
        wire: list[JsonObject] = [row.payload for row in all_rows]
        (case.root / "wire.json").write_text(json.dumps(wire), encoding="utf-8")
        violations: list[str] = []

        def expect(condition: bool, message: str) -> None:
            if not condition:
                violations.append(message)

        restored = (first, second, final_original)
        expect(before.progress_matches and before.path_safe and before.unique_identity,
               "pre-crash progress/path/identity disagrees with committed operation")
        expect(all(row.unique_identity and row.path_safe and row.task.submission_id == before.task.submission_id
                   and row.submission.input_entry_id == before.submission.input_entry_id
                   and row.model_requests == before.model_requests for row in restored),
               "original request/submission/operation identity changed")
        expected_recovered = 1 if boundary == "response_saved" or boundary == "wire_received" or (
            boundary == "request_ready" and not revoked) else 0
        expect(first_count == expected_recovered and second_count == first_count, "unexpected or repeated original HTTP send")
        expect(second.metrics() == first.metrics() and final_original.metrics() == first.metrics(),
               "second cold open or independent task changed the original conclusion")
        if boundary == "response_saved" or (boundary == "request_ready" and not revoked):
            expect(all(row.completed() for row in restored), "saved/eligible original run did not complete")
        else:
            expect(all(row.no_false_completion() for row in restored), "refused/uncertain original run fabricated completion")
        if boundary == "request_ready" and revoked:
            expect(all(not row.responses and all(_sent_state(effect) == "not_sent" for effect in row.effects)
                       for row in restored), "refused prepared request fabricated a provider response or sent receipt")
        if boundary in {"intent_admitted", "wire_received"}:
            expect(all(len(row.effects) == 1 and all(_sent_state(effect) in {"unsettled", "maybe_sent"}
                                                   for effect in row.effects) for row in restored),
                   "unknown original send was lost, declared NOT_SENT or replaced")
        if boundary == "response_saved":
            expect(len(before.responses) == 1 and all(row.model_responses == before.model_responses for row in restored),
                   "original committed response was lost or replaced")
        if revoked:
            expect(all(not _has_hint(row) for row in capture.rows()[seed_count + len(initial):]),
                   "revoked hint reached a physical attempt after crash")
        expect(fresh.completed() and len(fresh_requests) == 1, "independent normal fresh run did not complete exactly once")
        expect(all(_has_hint(row) == (not revoked) for row in fresh_requests), "fresh run hint does not match scoped control")
        result: dict[str, object] = {
            "scope": scope, "boundary": boundary, "revoked": revoked, "fixture_valid": True,
            "model_called": False, "initial_http": len(initial), "after_first_recovery_http": first_count,
            "after_second_recovery_http": second_count, "fresh_http": len(fresh_requests),
            "fresh_hint_count": sum(_has_hint(row) for row in fresh_requests), "before": before.metrics(),
            "first": first.metrics(), "second": second.metrics(), "fresh": fresh.metrics(),
            "final_original": final_original.metrics(), "violations": violations,
        }
        (case.root / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


@dataclass
class Options(argparse.Namespace):
    self_test: bool = False
    child: str = ""
    state_root: Path | None = None
    scope: str = "user"
    boundary: str = "request_ready"
    endpoint: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--child", choices=STAGES, default="", help=argparse.SUPPRESS)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--scope", choices=SCOPES, default="user")
    parser.add_argument("--boundary", choices=BOUNDARIES, default="request_ready")
    parser.add_argument("--endpoint", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(namespace=Options())
    if args.child:
        if args.state_root is None or not args.endpoint:
            parser.error("--child requires --state-root and --endpoint")
        _child(Case(args.state_root, args.scope, args.boundary, False), args.child, args.endpoint)
        return 0
    if not args.self_test:
        parser.print_help()
        return 2
    root = args.state_root or Path(tempfile.mkdtemp(prefix="codey-app-http-crash-"))
    failed = 0
    for scope in SCOPES:
        for boundary in BOUNDARIES:
            for revoked in (False, True):
                cell = run_cell(root / f"{scope}-{boundary}-{revoked}", scope, boundary, revoked)
                print("APP_HTTP_CRASH_CELL " + json.dumps(cell, sort_keys=True), flush=True)
                if cell["violations"] != []:
                    failed += 1
    print(f"normal App crash recovery: {24 - failed}/24 passed; state={root}")
    return int(failed != 0)


if __name__ == "__main__":
    raise SystemExit(main())
