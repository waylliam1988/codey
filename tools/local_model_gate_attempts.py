"""Disk records and process deadlines for the manual local-model gate.

This module does not implement an agent loop or change provider semantics.
Every case uses the production provider and headless entry in a child process.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from codey.providers.local_openai import LocalOpenAIProvider
from codey.runtime.core.cancellation import start_process, wait_process

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVE_CASES = frozenset({"create", "edit", "references", "hybrid", "auto", "tests"})

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


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_run_dir(parent: Path) -> Path:
    directory = parent / (time.strftime("local-model-release-%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10])
    directory.mkdir(parents=True, exist_ok=False)
    return directory


class RecordingProvider(LocalOpenAIProvider):
    """Observe parsed provider exchanges, including failed logical sends.

    Internal HTTP retries remain production behavior. Logical sends and
    physical HTTP attempts are recorded separately from the actual wire
    bytes. No authorization headers are saved.
    """
    def __init__(self, target: GateTarget, directory: Path):
        super().__init__(
            target.base_url, target.model, temperature=target.temperature, timeout=target.request_timeout,
            context_window_tokens=target.context_window_tokens,
            context_reserve_tokens=target.context_reserve_tokens,
            context_keep_recent_tokens=target.context_keep_recent_tokens,
        )
        self.directory = directory
        self.exchange_number = 0
        self.recorder_id = uuid.uuid4().hex

    def _post_chat(self, messages, tools=None, *, timeout=None):
        self.exchange_number += 1
        number = self.exchange_number
        started = time.perf_counter()
        try:
            body = super()._post_chat(messages, tools, timeout=timeout)
        except Exception as exc:
            self._record({"type": "error", "exchange": number, "error": f"{type(exc).__name__}: {exc}",
                          "seconds": time.perf_counter() - started})
            raise
        self._record({"type": "response", "exchange": number, "payload": body,
                      "seconds": time.perf_counter() - started})
        return body

    def _observe_http_attempt(self, *, attempt, data, phase, response_bytes, seconds):
        if phase == "request" and attempt == 1:
            self._record({"type": "request", "exchange": self.exchange_number, "payload": json.loads(data)})
        self._record({
            "type": "wire_attempt", "exchange": self.exchange_number, "attempt": attempt, "phase": phase,
            "request_sha256": hashlib.sha256(data).hexdigest(), "request_bytes": len(data),
            "response_bytes": response_bytes, "seconds": seconds,
        })

    def _record(self, row: dict) -> None:
        row = {**row, "recorder_id": self.recorder_id}
        with (self.directory / "provider.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_provider(target: GateTarget, directory: Path) -> RecordingProvider:
    return RecordingProvider(target, directory)


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


def _has_truncation(result: dict) -> bool:
    metrics = result.get("provider_metrics")
    if not isinstance(metrics, dict):
        return False
    return any(str(reason or "").lower() == "length" for reason in metrics.get("active_finish_reasons", ()))


def _failure_kind(result: dict) -> str:
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


def annotate_result(result: dict) -> dict:
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


def _provider_metrics(directory: Path) -> dict:
    path = directory / "provider.jsonl"
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = raw.splitlines()
    rows = []
    incomplete = False
    for index, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                incomplete = True  # The owned worker was interrupted mid-write.
            else:
                raise
    requests = {(row.get("recorder_id"), row["exchange"]): row["payload"] for row in rows if row["type"] == "request"}
    response_rows = [row for row in rows if row["type"] == "response"]
    responses = [row["payload"] for row in response_rows]
    active_reasons, terminal_reasons = [], []
    for row in response_rows:
        request = requests.get((row.get("recorder_id"), row.get("exchange")), {})
        messages = request.get("messages") or []
        terminal = (not request.get("tools") and request.get("max_tokens") == 1
                    and bool(messages) and messages[-1].get("role") == "tool")
        reasons = terminal_reasons if terminal else active_reasons
        reasons.extend(choice.get("finish_reason") for choice in row["payload"].get("choices", []))
    usage: Counter = Counter()
    for body in responses:
        for key, value in (body.get("usage") or {}).items():
            if type(value) is int:
                usage[key] += value
    return {
        "logical_sends": sum(row["type"] == "request" for row in rows),
        "http_attempts": sum(row["type"] == "wire_attempt" and row.get("phase") == "request" for row in rows),
        "http_retries": sum(row["type"] == "wire_attempt" and row.get("phase") == "request" and row.get("attempt", 0) > 1 for row in rows),
        "capture_incomplete": incomplete,
        "reported_models": sorted({str(body["model"]) for body in responses if body.get("model")}),
        "usage": dict(usage) if any("usage" in body for body in responses) else None,
        "finish_reasons": [choice.get("finish_reason") for body in responses for choice in body.get("choices", [])],
        "active_finish_reasons": active_reasons,
        "terminal_finish_reasons": terminal_reasons,
        "output_budget": "active turns: server_default; terminal receipts: max_tokens=1",
    }


def _child_result(directory: Path, completed) -> dict:
    (directory / "worker.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"gate worker exited {completed.returncode}; see worker.log")
    result = json.loads((directory / "worker-result.json").read_text(encoding="utf-8"))
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("worker result must contain an exact boolean ok")
    return result


def run_case_process(case: str, directory: Path, target: GateTarget, *, timeout: float) -> dict:
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
        try:
            shutil.copytree(project, directory / "project", symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(state, directory / "state", symlinks=True)
            result["provider_metrics"] = _provider_metrics(directory)
            if result.get("ok") is True and result["provider_metrics"]["capture_incomplete"]:
                raise ValueError("successful worker has a truncated provider record")
        except Exception as exc:
            artifact_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            for temporary in (project, state):
                try:
                    shutil.rmtree(temporary)
                except OSError as exc:
                    artifact_errors.append(f"cleanup {temporary}: {exc}")
        if artifact_errors:
            result["ok"] = False
            result["artifact_errors"] = artifact_errors
            if not result.get("failure_stage"):
                result["failure_stage"] = "artifact_error"
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


def _attempt_matrix(results: list[dict], expected_cases: tuple[str, ...] | None, repeat: int | None) -> dict:
    if expected_cases is None and repeat is None:
        return {"matrix_complete": True, "missing_attempts": [], "duplicate_attempts": [],
                "unexpected_attempts": [], "invalid_attempts": []}
    if (expected_cases is None or type(repeat) is not int or repeat < 1
            or not expected_cases or any(type(case) is not str or not case for case in expected_cases)
            or len(set(expected_cases)) != len(expected_cases)):
        raise ValueError("matrix requires unique nonempty cases and a positive integer repeat")
    expected = {(case, attempt) for case in expected_cases for attempt in range(1, repeat + 1)}
    observed: Counter = Counter()
    invalid = []
    for index, row in enumerate(results):
        case, attempt = row.get("case"), row.get("attempt")
        if type(case) is not str or type(attempt) is not int or attempt < 1:
            invalid.append(index)
        else:
            observed[(case, attempt)] += 1
    def slots(values):
        return [{"case": case, "attempt": attempt} for case, attempt in sorted(values)]
    missing = slots(expected - observed.keys())
    duplicates = slots(key for key, count in observed.items() if count > 1)
    unexpected = slots(observed.keys() - expected)
    return {"matrix_complete": not (missing or duplicates or unexpected or invalid),
            "missing_attempts": missing, "duplicate_attempts": duplicates,
            "unexpected_attempts": unexpected, "invalid_attempts": invalid}


def summarize(
    results: list[dict], *, expected_cases: tuple[str, ...] | None = None,
    repeat: int | None = None,
) -> dict:
    objective = [row for row in results if row.get("scope") == "objective_task"]
    attempt_matrix = _attempt_matrix(results, expected_cases, repeat)
    matrix: dict[str, dict[str, dict[str, int]]] = {}
    root_causes: Counter = Counter()
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


def run_attempts(cases, directory: Path, target: GateTarget, *, repeat: int, timeout: float) -> list[dict]:
    results = []
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
