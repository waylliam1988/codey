"""Disk records and process deadlines for the manual local-model gate.

This module does not implement an agent loop or change provider semantics.
Every case uses the production provider and headless entry in a child process.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType
from typing import NotRequired, Protocol, TypedDict, Unpack, cast

from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import AttemptBudget
from codey.providers.api_transport import AttemptControl
from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult
from codey.providers.local_response_codec import normalize_local_reply
from codey.providers.local_tool_compat import safe_native_tools
from codey.providers.zen.connection import ZenProvider
from codey.runtime.core.cancellation import CapturedProcess, start_process, wait_process
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_CASES = frozenset({"create", "edit", "references", "hybrid", "auto", "tests", "project_review"})
_TERMINAL_RECEIPT: ContextVar[bool] = ContextVar("gate_terminal_receipt", default=False)
_RECORDING_PATH: ContextVar[Path | None] = ContextVar("gate_recording_path", default=None)

# These labels are deliberately about the task contract, not implementation
# details. Unknown/new cases stay visible as their own kind until the matrix
# explicitly assigns them a stable category.
CASE_TASK_KINDS = {
    "chat": "chat",
    "read": "read_only",
    "create": "feature",
    "edit": "bug_fix",
    "references": "refactor",
    "hybrid": "browser",
    "discussion": "conversation",
    "planning": "long_horizon",
    "auto": "feature",
    "ghost": "recovery",
    "tests": "tests",
    "research": "research",
    "recovery": "recovery",
    "review": "review",
    "project_review": "coding_review",
    "shell_approve": "approval_continuation",
    "shell_deny": "approval_continuation",
    "shell_failure": "approval_continuation",
}
FAILURE_KINDS = frozenset({
    "none",
    "timeout",
    "truncation",
    "tool_error",
    "process_kill",
    "provider_restart",
    "duplicate_event",
    "resume",
    "provider_error",
    "unclassified",
})
ROOT_CAUSE_CLASSES = frozenset({
    "none",
    "undetermined",
    "model_boundary",
    "provider_boundary",
    "gate_defect",
    "production_defect",
    "environment",
    "unclassified",
})


def _mapping(value: object) -> Mapping[str, object]:
    return decode_json_object(value) if value is not None else {}


def _objects(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _as_int(value: object, default: int) -> int:
    return value if type(value) is int else default


class _RecorderIdentity(Protocol):
    recorder_id: str


class _RuntimeRecorder(Protocol):
    _gate_recorder_id: str


class _AttemptObserver(Protocol):
    def __call__(
        self, *, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float,
    ) -> None: ...


class _GenerationOptions(TypedDict):
    timeout: float
    observe: _AttemptObserver
    cancelled: NotRequired[Callable[[], bool]]
    opened: NotRequired[Callable[[object], None]]
    on_event: NotRequired[Callable[[dict[str, object]], None]]
    on_control: NotRequired[Callable[[AttemptControl], None]]
    attempt_budget: NotRequired[AttemptBudget | None]
    idle_timeout: NotRequired[float | None]
    http_session: NotRequired[object]
    transport_options: NotRequired[Mapping[str, object] | None]


@dataclass(frozen=True)
class GateTarget:
    base_url: str
    model: str
    context_window_tokens: int
    context_reserve_tokens: int
    context_keep_recent_tokens: int
    protocol: str = "native"
    temperature: float = 0.0
    request_timeout: float = 600.0
    provider_id: str = "local"
    api_selection: JsonObject = field(default_factory=dict)
    api_protocol: str = "openai-completions"
    turn_budget: int = 0


def write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_run_dir(parent: Path) -> Path:
    directory = parent / (time.strftime("local-model-release-%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10])
    directory.mkdir(parents=True, exist_ok=False)
    return directory


class RecordingProvider(ApiProvider):
    """Observe parsed provider exchanges, including failed logical sends.

    Unknown HTTP outcomes are not automatically resent. Logical sends and
    physical HTTP attempts are recorded separately from the actual wire
    bytes. No authorization headers are saved.
    """
    def __init__(self, target: GateTarget, directory: Path) -> None:
        native_tools = safe_native_tools(target.base_url, target.model, api_protocol=target.api_protocol,
                                         requested=target.protocol == "native")
        parser_compat = target.protocol == "native" and not native_tools
        super().__init__(
            target.base_url, target.model, temperature=target.temperature, timeout=target.request_timeout,
            context_window_tokens=target.context_window_tokens,
            context_reserve_tokens=target.context_reserve_tokens,
            context_keep_recent_tokens=target.context_keep_recent_tokens,
            api_protocol=target.api_protocol,
            native_tools=native_tools,
            text_tool_format="json_schema" if parser_compat else "plain",
            thinking_enabled=False if parser_compat else None,
            text_decoder=normalize_local_reply,
        )
        self.directory = directory
        self.exchange_number = 0
        self.recorder_id = uuid.uuid4().hex

    def _generate(
        self,
        messages: list[dict[str, object]],
        tools: list[ProviderToolDefinition] | None = None,
        *,
        timeout: float | None = None,
        checkpoints: list[dict[str, object]] | None = None,
        tool_call_json: bool = False,
        tool_call_schema: dict[str, object] | None = None,
        tool_call_contract: str = "",
        tool_call_observations: str = "",
        tool_call_history_groups: int = 0,
        output_token_limit: int = 0,
        require_tool_call: bool = False,
    ) -> dict[str, object]:
        self.exchange_number += 1
        number = self.exchange_number
        started = time.perf_counter()
        try:
            body: dict[str, object] = super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints,
                tool_call_json=tool_call_json, tool_call_schema=tool_call_schema,
                tool_call_contract=tool_call_contract, tool_call_observations=tool_call_observations,
                tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
                require_tool_call=require_tool_call)
        except Exception as exc:
            self._record({"type": "error", "exchange": number, "error": f"{type(exc).__name__}: {exc}",
                          "seconds": time.perf_counter() - started})
            raise
        self._record({"type": "response", "exchange": number, "payload": body,
                      "seconds": time.perf_counter() - started})
        return body

    def _observe_http_attempt(
        self, *, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float,
    ) -> None:
        if phase == "request" and attempt == 1:
            self._record({"type": "request", "exchange": self.exchange_number, "payload": json.loads(data),
                          "terminal": _TERMINAL_RECEIPT.get()})
        self._record({
            "type": "wire_attempt", "exchange": self.exchange_number, "attempt": attempt, "phase": phase,
            "request_sha256": hashlib.sha256(data).hexdigest(), "request_bytes": len(data),
            "response_bytes": response_bytes, "seconds": seconds,
        })

    def _record(self, row: Mapping[str, object]) -> None:
        row = {**row, "recorder_id": self.recorder_id}
        with (self.directory / "provider.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_provider(target: GateTarget, directory: Path) -> ApiProvider | ZenProvider:
    if target.provider_id != "local":
        from codey.providers.api_connections import open_selection
        from codey.runtime.core.api_selection import ApiRunSelection

        provider = cast(ApiProvider | ZenProvider, open_selection(ApiRunSelection.from_payload(target.api_selection)))
        recorder_id = uuid.uuid4().hex
        runtime = provider.runtime if isinstance(provider, ZenProvider) else provider
        cast(_RecorderIdentity, provider).recorder_id = recorder_id
        cast(_RuntimeRecorder, runtime)._gate_recorder_id = recorder_id
        runtime.timeout = target.request_timeout
        return provider
    return RecordingProvider(target, directory)


@contextmanager
def record_api_generations(path: Path) -> Iterator[None]:
    """Observe the production HTTP call for either protocol; never save headers."""
    import threading
    from unittest.mock import patch


    if _RECORDING_PATH.get() == path.resolve():
        yield
        return
    original = api_generation.generate
    lock = threading.Lock()
    counter = 0

    def append(row: Mapping[str, object]) -> None:
        with lock, path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def generate(
        url: str, payload: Mapping[str, object], headers: Mapping[str, str], **kwargs: Unpack[_GenerationOptions],
    ) -> dict[str, object]:
        nonlocal counter
        with lock:
            counter += 1
            exchange = counter
        callback = kwargs.get("observe")
        runtime: object = getattr(callback, "__self__", None)
        identity: object = getattr(runtime, "_gate_recorder_id", "ui")
        budget = kwargs.get("attempt_budget")
        common: dict[str, object] = {"exchange": exchange, "recorder_id": identity, "model": payload.get("model"),
                  "terminal": _TERMINAL_RECEIPT.get(),
                  "logical_attempt": budget.used if budget is not None else 1}
        append({**common, "type": "request", "payload": payload})
        def observe(*, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float) -> None:
            append({**common, "type": "wire_attempt", "attempt": attempt, "phase": phase,
                    "response_bytes": response_bytes, "seconds": seconds})
            if callback is not None:
                callback(attempt=attempt, data=data, phase=phase, response_bytes=response_bytes, seconds=seconds)
        kwargs["observe"] = observe
        started = time.perf_counter()
        try:
            body: dict[str, object] = original(url, payload, headers, **kwargs)
        except Exception as exc:
            append({**common, "type": "error", "error": f"{type(exc).__name__}: {exc}",
                    "http_status": getattr(exc, "status", None),
                    "upstream_request_id": getattr(exc, "upstream_request_id", ""),
                    "reason_code": getattr(exc, "reason_code", "")})
            raise
        append({**common, "type": "response", "payload": body, "seconds": time.perf_counter() - started})
        return body

    path.parent.mkdir(parents=True, exist_ok=True)
    original_acknowledge = ApiProvider.acknowledge_tool_results

    def acknowledge(
        provider: ApiProvider, results: list[ProviderToolResult], declared_tools: list[ProviderToolDefinition],
        timeout: float | None = None,
    ) -> AssistantTurn:
        token = _TERMINAL_RECEIPT.set(True)
        try:
            return original_acknowledge(provider, results, declared_tools, timeout)
        finally:
            _TERMINAL_RECEIPT.reset(token)

    token = _RECORDING_PATH.set(path.resolve())
    try:
        with patch.object(api_generation, "generate", generate), patch.object(ApiProvider, "acknowledge_tool_results", acknowledge):
            yield
    finally:
        _RECORDING_PATH.reset(token)


@contextmanager
def isolate_gate_secondary_models(target: GateTarget) -> Iterator[None]:
    """A pinned live gate must not call the user's unrelated secondary models."""
    from unittest.mock import patch

    from codey.app import provider_services

    with patch.object(provider_services, "provider_availability", return_value={target.provider_id: True}):
        yield


def case_scope(case: str) -> str:
    if case in OBJECTIVE_CASES:
        return "objective_task"
    if case in {"discussion", "planning"}:
        return "conversation_safety"
    if case == "recovery":
        return "recovery_safety"
    return "control_plane" if case == "ghost" else "protocol"


def case_task_kind(case: str) -> str:
    """Return the stable task-axis label for a gate case."""
    return CASE_TASK_KINDS.get(str(case), str(case) or "unclassified")


def _has_truncation(result: Mapping[str, object]) -> bool:
    metrics = result.get("provider_metrics")
    if not isinstance(metrics, dict):
        return False
    return any(str(reason or "").lower() in {"length", "max_output_tokens"}
               for reason in _objects(_mapping(metrics).get("active_finish_reasons")))


def _failure_kind(result: Mapping[str, object]) -> str:
    if result.get("ok") is True:
        return "none"
    stage = str(result.get("failure_stage") or "")
    if stage == "case_timeout":
        return "timeout"
    if "provider_restart" in stage:
        return "provider_restart"
    if "duplicate_event" in stage:
        return "duplicate_event"
    if "resume" in stage or "recovery" in stage:
        return "resume"
    if "process" in stage or "kill" in stage:
        return "process_kill"
    if _has_truncation(result):
        return "truncation"
    if "tool" in stage:
        return "tool_error"
    if "provider" in stage:
        return "provider_error"
    if "research_environment" in stage:
        return "tool_error"
    return "unclassified"


def annotate_result(result: Mapping[str, object]) -> dict[str, object]:
    """Attach observed matrix labels without guessing a root cause.

    A truncation can be caused by model behavior, provider limits, or an
    integration mistake. Until a deterministic comparison proves which one,
    the root cause remains ``undetermined``.
    """
    annotated = dict(result)
    annotated.setdefault("task_kind", case_task_kind(str(result.get("case") or "")))
    annotated.setdefault("failure_kind", _failure_kind(annotated))
    inferred = "none" if annotated["failure_kind"] == "none" else "undetermined"
    if annotated.get("failure_stage") == "research_environment_unavailable":
        inferred = "environment"
    annotated.setdefault("root_cause_class", inferred)
    return annotated


def _provider_metrics(directory: Path) -> dict[str, object]:
    path = directory / "provider.jsonl"
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = raw.splitlines()
    rows: list[dict[str, object]] = []
    incomplete = False
    for index, line in enumerate(lines):
        try:
            rows.append(dict(parse_json_object(line)))
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                incomplete = True  # The owned worker was interrupted mid-write.
            else:
                raise
    request_rows = {(row.get("recorder_id"), row["exchange"]): row for row in rows if row["type"] == "request"}
    response_rows = [row for row in rows if row["type"] == "response"]
    responses = [_mapping(row["payload"]) for row in response_rows]
    active_reasons: list[object] = []
    terminal_reasons: list[object] = []
    budgets: dict[str, set[object]] = {"active": set(), "terminal": set()}

    def terminal_request(row: Mapping[str, object], request: Mapping[str, object]) -> bool:
        messages = _objects(request.get("messages"))
        last_message = _mapping(messages[-1]) if messages else {}
        return row.get("terminal") is True or (not request.get("tools") and request.get("max_tokens") == 1
            and bool(messages) and last_message.get("role") == "tool")

    def finish_reasons(body: Mapping[str, object]) -> list[object]:
        if "status" in body:
            return [_mapping(body.get("incomplete_details")).get("reason") or body["status"]]
        return [_mapping(choice).get("finish_reason") for choice in _objects(body.get("choices"))]

    for row in rows:
        if row["type"] == "request":
            request = _mapping(row["payload"])
            budget = request.get("max_output_tokens", request.get("max_completion_tokens", request.get("max_tokens", "server_default")))
            budgets["terminal" if terminal_request(row, request) else "active"].add(budget)
    for row in response_rows:
        request_row = request_rows.get((row.get("recorder_id"), row.get("exchange")), {})
        request = _mapping(request_row.get("payload", {}))
        terminal = terminal_request(request_row, request)
        reasons = terminal_reasons if terminal else active_reasons
        reasons.extend(finish_reasons(_mapping(row["payload"])))
    usage: Counter[str] = Counter()
    for body in responses:
        for key, value in _mapping(body.get("usage")).items():
            if type(value) is int:
                usage[key] += value
    return {
        "logical_sends": sum(row["type"] == "request" and row.get("logical_attempt", 1) == 1 for row in rows),
        "http_attempts": sum(row["type"] == "wire_attempt" and row.get("phase") == "request" for row in rows),
        "http_retries": sum(row["type"] == "wire_attempt" and row.get("phase") == "request"
            and max(_as_int(row.get("logical_attempt"), 1), _as_int(row.get("attempt"), 0)) > 1 for row in rows),
        "capture_incomplete": incomplete,
        "reported_models": sorted({str(body["model"]) for body in responses if body.get("model")}),
        "usage": dict(usage) if any("usage" in body for body in responses) else None,
        "finish_reasons": [reason for body in responses for reason in finish_reasons(body)],
        "active_finish_reasons": active_reasons,
        "terminal_finish_reasons": terminal_reasons,
        "output_budget": {key: sorted(value, key=str) for key, value in budgets.items()},
    }


def _child_result(directory: Path, completed: CapturedProcess) -> dict[str, object]:
    (directory / "worker.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"gate worker exited {completed.returncode}; see worker.log")
    result: dict[str, object] = dict(parse_json_object((directory / "worker-result.json").read_text(encoding="utf-8")))
    if type(result.get("ok")) is not bool:
        raise ValueError("worker result must contain an exact boolean ok")
    return result


def run_case_process(case: str, directory: Path, target: GateTarget, *, timeout: float) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=False)
    # Project and state must live outside the Codey Git checkout. Otherwise
    # production change collection would measure the runner's own changes.
    project = Path(tempfile.mkdtemp(prefix=f"codey-gate-{case}-")).resolve()
    state = Path(tempfile.mkdtemp(prefix="codey-gate-state-")).resolve()
    started = time.perf_counter()
    result = {"case": case, "ok": False, "scope": case_scope(case)}
    try:
        write_json(directory / "input.json", {"target": asdict(target), "project": str(project), "state": str(state)})
        command = [sys.executable, "-B", str(REPO_ROOT / "tools/local_model_release_gate.py"),
                   "--worker-case", case, "--worker-dir", str(directory)]
        proc, job = start_process(command, cwd=REPO_ROOT)
        completed = wait_process(proc, job, command, timeout, capture_limit_bytes=256_000)
        result = _child_result(directory, completed)
        if result.get("case") != case:
            raise ValueError("worker returned a different case")
    except subprocess.TimeoutExpired:
        result.update(ok=False, failure_stage="case_timeout", error=f"case exceeded {timeout}s")
    except Exception as exc:
        result.update(ok=False, failure_stage="harness_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        artifact_errors = []
        artifact_warnings = []
        try:
            shutil.copytree(project, directory / "project", symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        except Exception as exc:
            artifact_errors.append(f"{type(exc).__name__}: {exc}")
        try:
            shutil.copytree(state, directory / "state", symlinks=True)
        except Exception as exc:
            if _is_transient_state_snapshot_error(exc):
                artifact_warnings.append(f"{type(exc).__name__}: {exc}")
            else:
                artifact_errors.append(f"{type(exc).__name__}: {exc}")
        try:
            result["provider_metrics"] = _provider_metrics(directory)
            metrics = _mapping(result["provider_metrics"])
            if result.get("ok") is True and metrics.get("capture_incomplete"):
                raise ValueError("successful worker has a truncated provider record")
        except Exception as exc:
            artifact_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            for temporary in (project, state):
                try:
                    _remove_case_temp_tree(temporary)
                except OSError as exc:
                    artifact_errors.append(f"cleanup {temporary}: {exc}")
        if artifact_errors:
            result["ok"] = False
            result["artifact_errors"] = artifact_errors
            if not result.get("failure_stage"):
                result["failure_stage"] = "artifact_error"
        if artifact_warnings:
            result["artifact_warnings"] = artifact_warnings
        result = annotate_result(result)
        result.update(case=case, scope=case_scope(case), model=target.model, base_url=target.base_url,
                      seconds=round(time.perf_counter() - started, 3), project=str(directory / "project"),
                      artifacts=str(directory))
        if result.get("root_cause_class") == "environment":
            result.setdefault("root_cause_evidence", {
                "events": "events.jsonl", "provider": "provider.jsonl", "result": "result.json",
            })
        write_json(directory / "result.json", result)
    return result


def _is_transient_state_snapshot_error(exc: Exception) -> bool:
    """Recognize durable-state files removed during a best-effort artifact copy.

    Recovery cleanup may remove a baseline between ``copytree``'s directory
    scan and its file copy.  The state snapshot is diagnostic evidence; a
    missing ephemeral baseline must remain visible as a warning without
    turning an otherwise successful production run into a failed task.  Any
    other copy failure still fails the gate.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    if not isinstance(exc, shutil.Error) or not exc.args:
        return False
    entries = exc.args[0]
    if not isinstance(entries, list) or not entries:
        return False
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            return False
        source, _destination, reason = entry
        # copytree stringifies OS exceptions, and worker output may use a
        # different locale/encoding. Error identifiers survive that conversion.
        missing = re.search(r"\[(?:WinError [23]|Errno 2)\]", str(reason)) is not None
        if Path(source).exists() or not (missing or "No such file" in str(reason) or "系统找不到" in str(reason)):
            return False
    return True


def _remove_case_temp_tree(root: Path) -> None:
    """Remove only a gate-created temp tree; retry read-only Git files once."""
    import stat

    expected = Path(tempfile.gettempdir()).resolve()
    resolved = root.resolve()
    if root.is_symlink() or resolved.parent != expected or not resolved.name.startswith("codey-gate-"):
        raise ValueError("gate cleanup target is not an isolated temp tree")

    def retry_readonly(
        operation: Callable[[str], object], filename: str,
        exception_info: tuple[type[BaseException], BaseException, TracebackType | None],
    ) -> None:
        error = exception_info[1]
        path = Path(filename)
        if not isinstance(error, PermissionError) or operation not in {os.unlink, os.remove, os.rmdir}:
            raise error
        path.resolve().relative_to(resolved)
        path.chmod(path.stat().st_mode | stat.S_IWRITE | stat.S_IWUSR)
        operation(filename)

    shutil.rmtree(resolved, onerror=retry_readonly)


def _attempt_matrix(
    results: list[dict[str, object]], expected_cases: tuple[str, ...] | None, repeat: int | None,
) -> dict[str, object]:
    if expected_cases is None and repeat is None:
        return {"matrix_complete": True, "missing_attempts": [], "duplicate_attempts": [],
                "unexpected_attempts": [], "invalid_attempts": []}
    if (expected_cases is None or type(repeat) is not int or repeat < 1
            or not expected_cases or any(type(case) is not str or not case for case in expected_cases)
            or len(set(expected_cases)) != len(expected_cases)):
        raise ValueError("matrix requires unique nonempty cases and a positive integer repeat")
    expected = {(case, attempt) for case in expected_cases for attempt in range(1, repeat + 1)}
    observed: Counter[tuple[str, int]] = Counter()
    invalid: list[int] = []
    for index, row in enumerate(results):
        case, attempt = row.get("case"), row.get("attempt")
        if type(case) is not str or type(attempt) is not int or attempt < 1:
            invalid.append(index)
        else:
            observed[(case, attempt)] += 1
    def slots(values: Iterable[tuple[str, int]]) -> list[dict[str, object]]:
        return [{"case": case, "attempt": attempt} for case, attempt in sorted(values)]
    missing = slots(expected - observed.keys())
    duplicates = slots(key for key, count in observed.items() if count > 1)
    unexpected = slots(observed.keys() - expected)
    return {"matrix_complete": not (missing or duplicates or unexpected or invalid),
            "missing_attempts": missing, "duplicate_attempts": duplicates,
            "unexpected_attempts": unexpected, "invalid_attempts": invalid}


def summarize(
    results: list[dict[str, object]], *, expected_cases: tuple[str, ...] | None = None,
    repeat: int | None = None,
) -> dict[str, object]:
    objective = [row for row in results if row.get("scope") == "objective_task"]
    attempt_matrix = _attempt_matrix(results, expected_cases, repeat)
    matrix: dict[str, dict[str, dict[str, int]]] = {}
    root_causes: Counter[str] = Counter()
    for row in results:
        task_kind = str(row.get("task_kind") or "unclassified")
        failure_kind = str(row.get("failure_kind") or "unclassified")
        if failure_kind not in FAILURE_KINDS:
            raise ValueError(f"unsupported failure_kind: {failure_kind}")
        cell = matrix.setdefault(task_kind, {}).setdefault(
            failure_kind, {"attempts": 0, "passed": 0},
        )
        cell["attempts"] += 1
        if row.get("ok") is True:
            cell["passed"] += 1
        root_cause = str(row.get("root_cause_class") or "unclassified")
        if root_cause not in ROOT_CAUSE_CLASSES:
            raise ValueError(f"unsupported root_cause_class: {root_cause}")
        if root_cause not in {"none", "undetermined", "unclassified"}:
            evidence = row.get("root_cause_evidence")
            if not evidence:
                raise ValueError("root_cause_evidence is required for an adjudicated root cause")
        if root_cause != "none":
            root_causes[root_cause] += 1
    return {
        "ok": bool(results) and attempt_matrix["matrix_complete"] and all(row.get("ok") is True for row in results),
        "attempts": len(results), "passed": sum(row.get("ok") is True for row in results),
        **attempt_matrix,
        "matrix": matrix,
        "root_causes": dict(root_causes),
        "objective_tasks": {
            "attempts": len(objective), "passed": sum(row.get("ok") is True for row in objective),
            "artifacts_correct": sum(row.get("work_correct") is True for row in objective),
            "artifacts_observed": sum(type(row.get("work_correct")) is bool for row in objective),
        },
        "answer_quality": "not_evaluated",
        "failures": dict(Counter(row.get("failure_stage", "unclassified") for row in results if row.get("ok") is not True)),
        "results": results,
    }


def run_attempts(
    cases: tuple[str, ...], directory: Path, target: GateTarget, *, repeat: int, timeout: float,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for attempt in range(1, repeat + 1):
        for case in cases:
            case_dir = directory / f"{attempt:02d}-{case}"
            print(f"[gate] attempt={attempt} case={case}", flush=True)
            try:
                result = run_case_process(case, case_dir, target, timeout=timeout)
            except Exception as exc:
                case_dir.mkdir(exist_ok=True)
                result = {"case": case, "ok": False, "scope": case_scope(case), "failure_stage": "harness_error",
                          "error": f"{type(exc).__name__}: {exc}", "model": target.model, "base_url": target.base_url}
                result = annotate_result(result)
                write_json(case_dir / "result.json", result)
            result["attempt"] = attempt
            results.append(result)
            # Save progress after every attempt; a later interruption cannot
            # discard already completed failures or successes.
            write_json(
                directory / "summary.json",
                summarize(results, expected_cases=tuple(cases), repeat=repeat),
            )
            print(f"[gate] {case}: {'PASS' if result.get('ok') is True else 'FAIL'} "
                  f"{result.get('failure_stage', '')}", flush=True)
    return results
