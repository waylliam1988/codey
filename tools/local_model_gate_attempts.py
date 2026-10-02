"""Disk records and process deadlines for the manual local-model gate.

This module does not implement an agent loop or change provider semantics.
Every case uses the production provider and headless entry in a child process.
"""
from __future__ import annotations

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
OBJECTIVE_CASES = frozenset({"create", "edit", "references", "hybrid", "auto"})


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

    Internal HTTP retries remain production behavior. Records are logical
    sends, not a count of wire requests. No authorization headers are saved.
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

    def _post_chat(self, messages, tools=None, *, timeout=None):
        self.exchange_number += 1
        number = self.exchange_number
        request = {"model": self.model, "messages": messages, "temperature": self.temperature, "stream": False}
        if tools:
            request.update(tools=tools, tool_choice="auto")
        self._record({"type": "request", "exchange": number, "payload": request})
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

    def _record(self, row: dict) -> None:
        with (self.directory / "provider.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_provider(target: GateTarget, directory: Path) -> RecordingProvider:
    return RecordingProvider(target, directory)


def case_scope(case: str) -> str:
    if case in OBJECTIVE_CASES:
        return "objective_task"
    if case in {"discussion", "planning"}:
        return "conversation_safety"
    return "control_plane" if case == "ghost" else "protocol"


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
    responses = [row["payload"] for row in rows if row["type"] == "response"]
    usage: Counter = Counter()
    for body in responses:
        for key, value in (body.get("usage") or {}).items():
            if type(value) is int:
                usage[key] += value
    return {
        "logical_sends": sum(row["type"] == "request" for row in rows),
        "capture_incomplete": incomplete,
        "reported_models": sorted({str(body["model"]) for body in responses if body.get("model")}),
        "usage": dict(usage) if any("usage" in body for body in responses) else None,
        "finish_reasons": [choice.get("finish_reason") for body in responses for choice in body.get("choices", [])],
        "output_budget": "server_default (max_tokens is not sent)",
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
        result.update(case=case, scope=case_scope(case), model=target.model, base_url=target.base_url,
                      seconds=round(time.perf_counter() - started, 3), project=str(directory / "project"),
                      artifacts=str(directory))
        write_json(directory / "result.json", result)
    return result


def summarize(results: list[dict]) -> dict:
    objective = [row for row in results if row.get("scope") == "objective_task"]
    return {
        "ok": bool(results) and all(row.get("ok") is True for row in results),
        "attempts": len(results), "passed": sum(row.get("ok") is True for row in results),
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
                write_json(case_dir / "result.json", result)
            result["attempt"] = attempt
            results.append(result)
            # Save progress after every attempt; a later interruption cannot
            # discard already completed failures or successes.
            write_json(directory / "summary.json", summarize(results))
            print(f"[gate] {case}: {'PASS' if result.get('ok') is True else 'FAIL'} "
                  f"{result.get('failure_stage', '')}", flush=True)
    return results
