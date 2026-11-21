"""Frozen live candidate and held-out family trial through normal headless API."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from contextlib import closing
from http.client import HTTPResponse
from pathlib import Path
from urllib.request import urlopen

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.memory_routes import memory_action
from codey.memory.codec import canonical
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.providers.local_response_codec import normalize_local_reply
from codey.types.json_value import JsonObject, JsonValue, parse_json_object
from codey.workspace.context_admission import request_scope
from tests.manual.ghost_memory_routing.world_model_verification_review import SingleAttemptPolicy, parse_usage
from tools.local_model_gate_attempts import GateTarget, isolate_gate_secondary_models, record_api_generations
from tools.memory_continuity_probe import ENDPOINT, MODEL, backend_idle

TASKS: tuple[str, ...] = (
    "Show a minimal function that squares an integer, with one usage example.",
    "Give a complete tiny program that prints the integers from one to three.",
    "Write a small function that adds two integers and show how to call it.",
    "Show a minimal example that stores three names and prints each name.",
    "Give a small function that checks whether an integer is even, with an example.",
    "Show a tiny function that returns the greater of two numbers, with an example.",
    "Write a short complete example that counts down from three to one.",
    "Give a minimal function that doubles an integer, with one example call.",
)
EXTRACTION_TASKS: tuple[str, ...] = (
    "Please remember: for future code examples, I prefer Rust unless I explicitly request another language.",
    "Please remember: for future ordinary explanations, I prefer Spanish unless I ask otherwise.",
    "For future explanations, I prefer a detailed step-by-step style. Please remember this preference.",
    "My ongoing goal for this project is to learn how SQLite transactions work. Please remember that goal.",
    "For this one answer only, use Python. Say hello briefly.",
    "For this one answer only, keep the reply short. Explain a unit test.",
)


class Arguments(argparse.Namespace):
    evidence: Path


def connect(provider_id: str, *, port: int) -> ChatProvider:
    if provider_id != "local":
        raise ValueError("this trial permits only its frozen local provider")
    return ApiProvider(ENDPOINT, MODEL, native_tools=False, text_tool_format="json_schema", stream=False,
        temperature=0, timeout=120, thinking_enabled=False, context_window_tokens=262144,
        context_reserve_tokens=32768, context_keep_recent_tokens=32000, output_tokens=2048,
        text_decoder=normalize_local_reply, request_policy=SingleAttemptPolicy(), usage_parser=parse_usage)


def run(root: Path, identity: str, task: str, *, treatment: bool = False, extraction: bool = False) -> JsonObject:
    directory = root / identity
    directory.mkdir()
    response: HTTPResponse = urlopen("http://127.0.0.1:5001/api/extra/perf", timeout=5)
    with response:
        performance = parse_json_object(response.read().decode())
    if not backend_idle(performance):
        raise RuntimeError("backend_busy: no generation sent")
    home = directory / "state"
    project = directory / "project"
    project.mkdir()
    if treatment:
        with closing(AppContext(home, start_scheduler=False)) as state:
            state.durable.create_conversation("source")
            status, payload = memory_action(state, {"action": "propose", "session_id": "source", "scope_kind": "user",
                "kind": "preference", "text": "For future code examples use Rust unless I explicitly request another language.",
                "slot": "code.language", "value": "rust"})
            if status != 200:
                raise ValueError(str(payload))
            proposal, = state.memory.proposals("user")
            if memory_action(state, {"action": "accept", "session_id": "source", "scope_kind": "user",
                "proposal_id": proposal.proposal_id, "version": proposal.version})[0] != 200:
                raise ValueError("trial preference confirmation failed")
    def emit(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    started = time.monotonic()
    target = GateTarget(ENDPOINT, MODEL, 262144, 32768, 32000)
    with record_api_generations(directory / "wire.jsonl"), isolate_gate_secondary_models(target):
        outcome = run_headless(HeadlessRequest(project, task, intent="chat", session_id="trial", provider_id="local",
            max_turns=1, state_home=home, model_selection={"model": MODEL, "base_url": ENDPOINT}),
            emit_jsonl=emit, connect_provider=connect)
    with closing(AppContext(home, start_scheduler=False)) as state:
        record = state.durable.task(outcome.run_id)
        answer_id = state.durable.submission(record.submission_id).answer_entry_id
        answer = next((row.payload.get("text") for row in state.durable.context("trial") if row.entry_id == answer_id), "")
        proposals: list[JsonValue] = [proposal.item.to_payload() for proposal in state.memory.proposals(
            request_scope(str(project), "trial")) if proposal.item.item_id.startswith("candidate:")]
        exported = state.memory.export_state()
    wire = [parse_json_object(line) for line in (directory / "wire.jsonl").read_text(encoding="utf-8").splitlines()]
    requests = sum(row.get("type") == "request" for row in wire)
    replies = sum(row.get("type") == "response" for row in wire)
    text = answer if isinstance(answer, str) else ""
    expected_absent = extraction and identity.endswith(("4", "5"))
    passed = (bool(proposals) != expected_absent) if extraction else "fn " in text and ("->" in text or "println!" in text)
    report: JsonObject = {"id": identity, "treatment": treatment, "task": task, "answer": text, "run_id": outcome.run_id,
        "stop_reason": outcome.stop_reason, "exit_code": outcome.exit_code, "requests": requests, "responses": replies,
        "seconds": time.monotonic() - started, "proposals": proposals, "memory_state": exported,
        "behavior_proxy_passed": passed and outcome.exit_code == 0 and requests == replies == 1,
        "metric": "candidate presence/absence" if extraction else "Rust syntax proxy; semantics require manual inspection",
        "quality_claim": False, "usage": [row.get("payload") for row in wire if row.get("type") == "response"]}
    (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(namespace=Arguments())
    args.evidence.mkdir(parents=True, exist_ok=False)
    response: HTTPResponse = urlopen(ENDPOINT + "/models", timeout=5)
    with response:
        advertised = parse_json_object(response.read().decode())
    if MODEL not in json.dumps(advertised):
        raise ValueError("environment model differs from the frozen trial")
    hashes: JsonObject = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path("codey").rglob("*.py"))}
    protocol: JsonObject = {"source_sha256": hashes, "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tasks": list(TASKS), "extraction_tasks": list(EXTRACTION_TASKS), "model": MODEL, "endpoint": ENDPOINT,
        "temperature": 0, "max_turns": 1, "output_tokens": 2048, "context_tokens": 262144,
        "scope": "single model, nonstream, one generation per cell; manual seeded paired family trial plus separate unseeded extraction",
        "trial_gate": "normal default production consumption of confirmed memory; no monkeypatch or persisted promotion"}
    (args.evidence / "protocol.json").write_text(canonical(protocol) + "\n", encoding="utf-8")
    results: list[JsonValue] = []
    cells = [("extract-" + str(index), text, False, True) for index, text in enumerate(EXTRACTION_TASKS)]
    cells.extend((f"pair-{index}-{'memory' if enabled else 'control'}", task, enabled, False)
                 for index, task in enumerate(TASKS) for enabled in (False, True))
    for identity, task, enabled, extraction in cells:
        report: JsonObject
        try:
            report = run(args.evidence, identity, task, treatment=enabled, extraction=extraction)
        except Exception as exc:
            report = {"id": identity, "error": type(exc).__name__, "detail": str(exc), "behavior_proxy_passed": False}
        results.append(report)
        summary: JsonObject = {"cells": results, "expected": len(cells)}
        (args.evidence / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        progress: JsonObject = {"id": identity, "passed": report.get("behavior_proxy_passed"), "error": report.get("error")}
        print(json.dumps(progress), flush=True)
        if report.get("error"):
            break
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != value for path, value in hashes.items()):
        raise RuntimeError("production sources changed during the trial")
    if len(results) != len(cells):
        raise RuntimeError("trial is incomplete; original failure and unsent cell count are retained")


if __name__ == "__main__":
    main()
