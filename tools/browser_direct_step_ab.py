"""Measure a direct-operation model view over unchanged production execution.

A sends the current production request. B removes the free-JavaScript CodeMode
branch from the model's current declarations and sampling schema. Internal
CodeMode remains available to production's existing direct-tool lowering; bound
file-copy inputs and explicitly requested CodeMode are never hidden. This is a
model-view experiment, not a new parser, permission boundary or executor.
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.operations.browser.browser_followups import codemode_only
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.kernel_prompt import _text_request_prompt
from codey.policies.tool_requirement import decode_answer
from codey.providers.base import ProviderToolDefinition, tools_from_specs
from codey.providers.text_tool_schema import text_tool_schema
from codey.runtime.core.models import ToolResult
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
)
from tools import browser_choice_presentation_ab as choices
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    Arm,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)


def schema_for(snapshot: TurnSnapshot) -> JsonObject:
    build: Callable[[list[ProviderToolDefinition]], object] = text_tool_schema
    return decode_json_object(build(list(tools_from_specs(snapshot.model_specs))))


def model_view(snapshot: TurnSnapshot, task: str) -> TurnSnapshot:
    """Render a narrower view; never pass this view to the kernel for execution."""
    spec = next((spec for spec in snapshot.model_specs if spec.name == "codemode"), None)
    if (codemode_only(task) or snapshot.input_binding is not None or snapshot.browser_copy is not None
            or spec is None or "code" not in dict(spec.parameters)):
        return snapshot
    return refresh_snapshot_contract(replace(snapshot, tool_names=tuple(
        name for name in snapshot.tool_names if name != "codemode")))


def project_request(payload: JsonObject, snapshot: TurnSnapshot, task: str) -> JsonObject:
    """Replace only the current contract and schema, preserving task/history."""
    view = model_view(snapshot, task)
    if view is snapshot:
        return canonical_json_object(payload)
    projected = canonical_json_object(payload)
    messages = projected.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Experiment requires a completions messages payload")
    contract = _text_request_prompt("", snapshot)
    candidate = _text_request_prompt("", view)
    replacements = 0
    for index, raw in enumerate(messages):
        message = decode_json_object(raw)
        text = message.get("content")
        if message.get("role") == "system" and isinstance(text, str) and contract in text:
            replacements += text.count(contract)
            message["content"] = text.replace(contract, candidate)
            messages[index] = message
    if replacements != 1:
        raise ValueError(f"Expected one current system contract, found {replacements}")
    response = decode_json_object(projected.get("response_format"))
    grammar = decode_json_object(response.get("json_schema"))
    if grammar.get("schema") != schema_for(snapshot):
        raise ValueError("Wire grammar differs from the captured production contract")
    grammar["schema"] = schema_for(view)
    response["json_schema"] = grammar
    projected["response_format"] = response
    return projected


class DirectProvider(choices.ChoiceProvider):
    def __init__(self, target: Target, directory: Path, arm: Arm,
                 prefixes: tuple[JsonObject, ...]) -> None:
        # Reuse recorded failure prefixes, never append the old action cards.
        super().__init__(target, directory, "A", prefixes)
        self.treatment = arm
        self.snapshot_source: Callable[[], TurnSnapshot] | None = None
        self.task = ""

    def configure_experiment(self, payload: dict[str, object]) -> None:
        super().configure_experiment(payload)
        if self.snapshot_source is None:
            raise ValueError("No captured production snapshot for the experiment")
        snapshot = self.snapshot_source()
        before = canonical_json_object(payload)
        after = project_request(before, snapshot, self.task) if self.treatment == "B" else before
        view = model_view(snapshot, self.task) if self.treatment == "B" else snapshot
        append_json(self.directory / "projection.jsonl", {
            "exchange": self.exchange, "arm": self.treatment,
            "production_tools": list(snapshot.tool_names), "model_tools": list(view.tool_names),
            "changed": after != before, "bound_input": snapshot.input_binding.kind if snapshot.input_binding else None,
            "production_contract_sha256": hashlib.sha256(_text_request_prompt("", snapshot).encode()).hexdigest(),
            "request_before_projection": before,
        })
        payload.clear()
        payload.update(after)


def fixture(name: str, repeat: int, *, explicit_js: bool = False) -> choices.Case:
    case = choices.fixture(name, repeat)
    task = case.task.replace("Use codemode only. ", "")
    if explicit_js:
        task += " Use codemode only."
    return replace(case, task=task)


def view_violations(directory: Path) -> list[JsonObject]:
    views: list[JsonObject] = [parse_json_object(line) for line in (directory / "projection.jsonl").read_text(
        encoding="utf-8").splitlines()] if (directory / "projection.jsonl").exists() else []
    allowed = {require_json_integer(row["exchange"], "exchange"): row["model_tools"] for row in views}
    violations: list[JsonObject] = []
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("type") != "response":
            continue
        payload = decode_json_object(row["payload"])
        raw = payload["choices"]
        if not isinstance(raw, list):
            raise ValueError("Response choices must be an array")
        text = require_json_string(decode_json_object(decode_json_object(raw[0])["message"])["content"], "content")
        try:
            action = decode_json_object(decode_answer(text))
        except (ValueError, TypeError):
            violations.append({"exchange": row["exchange"], "reason": "invalid_tool_response"})
            continue
        exchange = require_json_integer(row["exchange"], "exchange")
        names = allowed.get(exchange)
        if not isinstance(names, list) or action.get("tool") not in names:
            violations.append({"exchange": exchange, "tool": action.get("tool"), "reason": "outside_model_view"})
    return violations


def run_arm(directory: Path, case: choices.Case, arm: Arm, target: Target, *, max_turns: int = 10) -> JsonObject:
    before = production_source_digest()
    recording: DirectProvider | None = None
    original = KernelContext.request_snapshot
    state: JsonObject = {}

    def current_snapshot(context: KernelContext) -> TurnSnapshot:
        request = context.sink.request_context
        if request is None:
            raise ValueError("Provider request has no frozen kernel context")
        return request.snapshot

    def provider_factory(selected: Target, root: Path, *, fixed_acquisition: bool = False,
                         acquisition_action: JsonObject | None = None, stream: bool = False) -> DirectProvider:
        nonlocal recording
        assert not fixed_acquisition and acquisition_action is None and not stream
        recording = DirectProvider(selected, root, arm, case.prefixes)
        return recording

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        nonlocal state
        result = original(context, snapshot, delivered_results=delivered_results)
        assert recording is not None
        recording.snapshot_source = lambda: current_snapshot(context)
        recording.task = context.session.task_text
        resources = context.execution.research_tools
        browser = resources.browser_session if resources is not None else None
        if browser is not None:
            generation = browser.generation

            state = decode_json_object(browser.inspect_state("window.counts || {}"))
            assert browser.generation == generation
        return result

    with (patch.object(gate, "ExperimentProvider", provider_factory),
          patch.object(KernelContext, "request_snapshot", capture)):
        baseline = gate.run_case(directory, case.mode, target, task_transform=lambda _task: case.task,
            fixture_html=case.body, expected_amount=case.amount, expected_code=case.code,
            codemode_only=False, explicit_wait=False, declare_required_tools=False, max_turns=max_turns)
    results = [row.result for row in tool_receipts(directory / "state")]
    actions = choices.response_actions(directory / "provider.jsonl")
    successes = Counter(row.call.name for row in results if row.ok)
    evidence = choices.verify_evidence(results, str(baseline.get("answer", "")), (str(case.amount), case.code))
    file_correct = baseline.get("final_file_correct") is True if case.mode == "hybrid" else True
    no_replay = (successes["browser_click"] == case.clicks and successes["browser_fill"] == case.fills
                 and state.get("clicks") == case.clicks and state.get("fills") == case.fills and state.get("wrong") == 0)
    stable = before == production_source_digest()
    violations = view_violations(directory)
    passed = choices.successful_run(baseline, evidence, source_stable=stable,
        file_correct=file_correct, no_replay=no_replay) and not violations
    row: dict[str, object] = {"case": case.name, "arm": arm, "passed": passed,
        "stop_reason": str(baseline.get("stop_reason", "error")), "answer": str(baseline.get("answer", "")),
        "error": str(baseline.get("error", "")), "source_stable": stable,
        "scripted_prefixes": len(case.prefixes), "category": "explicit_js" if codemode_only(case.task) else
        "recovery" if case.prefixes else "natural", "tool_calls": dict(Counter(row.call.name for row in results)),
        "successful_calls": dict(successes), "no_repeated_actions": no_replay, "file_correct": file_correct,
        "evidence": evidence, "page_state": state, "view_violations": violations,
        "javascript_proposals": sum(action.get("tool") == "codemode" and
            "code" in decode_json_object(action.get("args", {})) for action in actions),
        "direct_proposals": sum(str(action.get("tool", "")).startswith("browser_") for action in actions),
        "failed_receipts": sum(not row.ok for row in results), "seconds": baseline.get("seconds"),
        **provider_metrics(directory / "provider.jsonl")}
    write_json(directory / "ab-summary.json", row)
    write_json(directory / "actions.json", actions)
    return canonical_json_object(row)


class Options(argparse.Namespace):
    run_dir: Path
    base_url: str
    repeats: int
    max_turns: int
    cases: list[str]
    explicit_js: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=10)
    default_cases: list[str] = list(choices.CASES)
    parser.add_argument("--cases", nargs="+", choices=choices.CASES, default=default_cases)
    parser.add_argument("--explicit-js", action="store_true")
    args = parser.parse_args(namespace=Options())
    if args.repeats < 1 or args.max_turns < 1:
        parser.error("repeats and max-turns must be positive")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = gate.select_target(90, args.base_url)
    choices.freeze(root)
    source = production_source_digest()
    (root / "frozen-experiment.py.txt").write_bytes(Path(__file__).read_bytes())
    write_json(root / "manifest.json", {"interface": "existing-direct-tools-model-view-v1", "cases": args.cases,
        "repeats": args.repeats, "max_turns": args.max_turns, "model": target.model, "seed_base": target.seed,
        "temperature": 0, "source_digest": source, "explicit_js_control": args.explicit_js,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "treatment": "B removes free-JS codemode only from the current model contract/schema; internal execution unchanged",
        "limits": "Model projection is not an execution permission barrier; out-of-view replies fail the experiment score. "
        "Controlled pages, no claim of arbitrary-web reliability. Explicit CodeMode and bound copy remain unchanged."})
    rows: list[JsonObject] = []
    for repeat in range(args.repeats):
        selected = replace(target, seed=target.seed + repeat * 1009)
        for index, name in enumerate(args.cases):
            case = fixture(name, repeat, explicit_js=args.explicit_js)
            order: tuple[Arm, Arm] = ("A", "B") if (repeat + index) % 2 == 0 else ("B", "A")
            for arm in order:
                if production_source_digest() != source:
                    raise RuntimeError("Production source changed during frozen experiment")
                choices.wait_for_idle(target.base_url)
                directory = root / f"r{repeat + 1}-{name}-{arm}"
                print(choices.render({"starting": directory.name}), flush=True)
                row = run_arm(directory, case, arm, selected, max_turns=args.max_turns)
                rows.append({"repeat": repeat + 1, **row})
                write_json(root / "summary.json", {"runs": rows, "source_before": source,
                    "source_after": production_source_digest()})
                print(choices.render({key: row[key] for key in ("case", "arm", "passed", "stop_reason", "model_calls")}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
