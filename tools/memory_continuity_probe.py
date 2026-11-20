"""Frozen, single-request pilot through normal headless Chat. Never imported by production."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from urllib.request import urlopen

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.memory_routes import memory_action
from codey.app.memory_setup import observe_terminal
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.providers.local_response_codec import normalize_local_reply
from codey.types.json_value import JsonObject, JsonValue, parse_json_object
from tests.manual.ghost_memory_routing.world_model_verification_review import SingleAttemptPolicy, parse_usage
from tools.local_model_gate_attempts import GateTarget, isolate_gate_secondary_models, record_api_generations

ENDPOINT = "http://127.0.0.1:5001/v1"
MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"


class ProbeArguments(argparse.Namespace):
    evidence: Path


class BackendBusy(RuntimeError):
    pass


def backend_idle(payload: JsonObject) -> bool:
    idle = payload.get("idle")
    return (idle is True or type(idle) is int and idle == 1) and payload.get("queue") == 0


@dataclass(frozen=True)
class Case:
    name: str
    preference: str
    task: str
    expected: str
    history: bool = False
    correction: bool = False

    def payload(self) -> JsonObject:
        return {"name": self.name, "preference": self.preference, "task": self.task,
                "expected": self.expected, "history": self.history, "correction": self.correction}


CASES = (
    Case("rust", "For code examples, default to Rust unless I request another language.",
         "Show the shortest complete example that prints a greeting.", "rust"),
    Case("python", "For code examples, default to Python unless I request another language.",
         "Show the shortest complete example that prints a greeting.", "python"),
    Case("spanish", "During ordinary explanations, please reply in Spanish unless I request another language.",
         "Explain what a unit test does in one sentence.", "spanish"),
    Case("project_history", "For PaperDock storage we chose SQLite because it works without a separate server.",
         "For PaperDock, remind me of our storage choice and the reason.", "history", history=True),
    Case("correction", "For code examples, default to Rust unless I request another language.",
         "Show the shortest complete example that prints a greeting.", "rust", correction=True),
    Case("current_override", "For code examples, default to Rust unless I request another language.",
         "For this answer use Python. Show the shortest complete example that prints a greeting.", "python"),
)


def score(expected: str, answer: str) -> bool:
    if expected == "rust":
        return bool(re.search(r"\bfn\s+main\s*\(", answer) and "println!" in answer)
    if expected == "python":
        return bool(re.search(r"\bprint\s*\(", answer)) and not bool(re.search(r"\bfn\s+main\s*\(", answer))
    if expected == "spanish":
        return bool(re.search(r"\b(prueba|pruebas|comprueba|verifica|funci[oó]n)\b", answer.casefold()))
    if expected == "history":
        return "sqlite" in answer.casefold() and bool(re.search(r"server|embedded|single.file|无需|服务器", answer.casefold()))
    raise ValueError("unknown frozen scorer")


def write(path: Path, payload: JsonObject) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed(home: Path, project: Path, case: Case) -> None:
    with closing(AppContext(home, start_scheduler=False)) as state:
        state.durable.create_conversation("history")
        if case.history:
            state.durable.admit_input(conversation_id="history", request_id="seed", task_id="seed",
                text=case.preference, provider_snapshot={}, task_state={"task": case.preference, "project": str(project)})
            state.durable.claim_task("seed")
            state.durable.complete_task("seed", "Recorded: SQLite avoids running a separate server.", state={})
            observe_terminal(state.memory, state.durable, "seed")
            return
        preferences = (["For code examples, default to Python unless I request another language."]
                       if case.correction else []) + [case.preference]
        for index, text in enumerate(preferences):
            result = memory_action(state, {"action": "propose", "session_id": "history", "scope_kind": "user",
                "kind": "preference", "text": text, "slot": "examples" if case.expected != "spanish" else "language",
                "request_id": f"seed:{index}"})
            if result[0] != 200:
                raise RuntimeError(str(result))
            proposal, = state.memory.proposals("user")
            status, payload = memory_action(state, {"action": "accept", "session_id": "history", "scope_kind": "user",
                "proposal_id": proposal.proposal_id, "version": proposal.version})
            if status != 200:
                raise RuntimeError(str(payload))


def run_cell(root: Path, case: Case, enabled: bool) -> JsonObject:
    directory = root / (case.name + ("-memory" if enabled else "-control"))
    directory.mkdir(exist_ok=False)
    response: HTTPResponse = urlopen("http://127.0.0.1:5001/api/extra/perf", timeout=5)
    with response:
        before = parse_json_object(response.read().decode())
    write(directory / "backend-before.json", before)
    if not backend_idle(before):
        raise BackendBusy("the local model is busy; this cell was not sent")
    home, project = directory / "state", directory / "workspace"
    project.mkdir()
    if enabled:
        seed(home, project, case)
    target = GateTarget(ENDPOINT, MODEL, 262144, 32768, 32000)

    def connect(provider_id: str, *, port: int) -> ChatProvider:
        if provider_id != "local":
            raise ValueError("only the frozen local model is allowed")
        return ApiProvider(ENDPOINT, MODEL, temperature=0, timeout=120, context_window_tokens=262144,
            context_reserve_tokens=32768, context_keep_recent_tokens=32000, output_tokens=2048,
            native_tools=False, text_tool_format="json_schema", thinking_enabled=False,
            text_decoder=normalize_local_reply, request_policy=SingleAttemptPolicy(), usage_parser=parse_usage)

    def emit(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    started = time.monotonic()
    with record_api_generations(directory / "wire.jsonl"), isolate_gate_secondary_models(target):
        result = run_headless(HeadlessRequest(project, case.task, intent="chat", provider_id="local",
            session_id="next", max_turns=1, state_home=home,
            model_selection={"model": MODEL, "base_url": ENDPOINT}), emit_jsonl=emit, connect_provider=connect)
    with closing(AppContext(home, start_scheduler=False)) as state:
        answer_id = state.durable.submission(state.durable.task(result.run_id).submission_id).answer_entry_id
        answer = next((entry.payload.get("text", "") for entry in state.durable.context("next")
                       if entry.entry_id == answer_id), "")
    text = answer if isinstance(answer, str) else ""
    wire = [parse_json_object(line) for line in (directory / "wire.jsonl").read_text(encoding="utf-8").splitlines()]
    requests = [row for row in wire if row.get("type") == "request"]
    replies = [row for row in wire if row.get("type") == "response"]
    completed = result.exit_code == 0 and len(requests) == 1 and len(replies) == 1
    report: JsonObject = {"case": case.name, "memory": enabled, "stop_reason": result.stop_reason,
        "run_id": result.run_id, "requests": len(requests), "replies": len(replies),
        "completed_single_request": completed, "passed": completed and score(case.expected, text),
        "metric": "frozen behavior proxy; manual semantic review required", "quality_claim": False,
        "seconds": time.monotonic() - started, "answer": text,
        "usage": [row.get("payload") for row in replies]}
    write(directory / "result.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(namespace=ProbeArguments())
    root: Path = args.evidence
    root.mkdir(parents=True, exist_ok=False)
    hashes: JsonObject = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in sorted(Path("codey").rglob("*.py"))}
    response: HTTPResponse = urlopen(ENDPOINT + "/models", timeout=5)
    with response:
        model = parse_json_object(response.read().decode())
    if MODEL not in json.dumps(model):
        raise ValueError("the live model differs from the frozen protocol")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
    write(root / "protocol.json", {"revision": revision, "source_sha256": hashes, "model": MODEL,
        "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "endpoint": ENDPOINT, "temperature": 0, "output_tokens": 2048, "max_turns": 1,
        "cases": [case.payload() for case in CASES], "kind": "small pilot; no generalization or learning-mechanism claim"})
    reports: list[JsonValue] = []
    for case in CASES:
        for enabled in (False, True):
            try:
                report = run_cell(root, case, enabled)
            except Exception as exc:
                report = {"case": case.name, "memory": enabled, "error": type(exc).__name__, "detail": str(exc), "passed": False}
            reports.append(report)
            write(root / "summary.json", {"cells": reports, "expected": len(CASES) * 2})
            progress: JsonObject = {key: report.get(key) for key in ("case", "memory", "passed", "requests", "stop_reason", "error")}
            print(json.dumps(progress), flush=True)
            if report.get("error") == "BackendBusy":
                return
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != value for path, value in hashes.items()):
        raise RuntimeError("production source changed during the frozen pilot")


if __name__ == "__main__":
    main()
