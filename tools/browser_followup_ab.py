"""Controlled autonomous ablations of browser exposure and result delivery.

Experiment process only. Automatic followups lower validated leaf inputs to a
fixed program in the EXISTING CodeMode runtime; every child uses ordinary
authorization, durable admission, settlement and evidence projection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Collection, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from unittest.mock import patch

from codey.operations import turn_snapshot
from codey.operations.browser import turn_surface
from codey.operations.kernel import kernel_context, kernel_protocol
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_guidance import task_result_context
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import KernelSessionView
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import _browser_body
from codey.protocols.text_turn import DecodedTextTurn, decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolResult
from codey.providers.task_prompt import TaskPromptSpan
from codey.providers.text_result_views import TextResultSpan
from codey.research.answer_contract import AnswerScope
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)
from tools import browser_leaf_exposure_ab as base
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)


@dataclass(frozen=True)
class Variant:
    leaves: tuple[str, ...] = base.DIRECT_TOOLS
    fill_observe: bool = False
    click_readback: bool = False
    reference_guidance: bool = False


VARIANTS = {
    "codemode": Variant(leaves=()),
    "direct": Variant(),
    "fill_observe": Variant(fill_observe=True),
    "click_readback": Variant(click_readback=True),
    "reference_guidance": Variant(reference_guidance=True),
    "combined": Variant(fill_observe=True, click_readback=True, reference_guidance=True),
    "read_only": Variant(leaves=("browser_snapshot", "browser_read")),
    "act_only": Variant(leaves=("browser_fill", "browser_click")),
}
REFERENCE_GUIDANCE = (
    "Citation namespaces: cite acquired web sources as [sN] in done.summary. "
    "done.knowledge_refs is only for actual knowledge objects with their acquired ids and revisions. "
    "A web source id is not a knowledge object id. If no knowledge object was acquired, omit knowledge_refs."
)


def harness_digest() -> str:
    paths = (Path(__file__), Path(base.__file__), Path("tools/completion_explanation_ab.py"),
             Path("tools/completion_artifact_audit.py"), Path("tools/browser_production_live_gate.py"))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def expose(snapshot: TurnSnapshot, session: KernelSessionView, variant: Variant) -> TurnSnapshot:
    candidate = base.expose_leaves(snapshot, session)
    names = tuple(name for name in candidate.tool_names if name in snapshot.tool_names or name in variant.leaves)
    inventory = tuple({name: None for name in (*names, *candidate.nested_tool_names)})
    specs, executors = {s.name: s for s in candidate.frozen_specs}, dict(candidate.custom_executors)
    return refresh_snapshot_contract(replace(candidate, tool_names=names,
        frozen_specs=tuple(specs[name] for name in inventory),
        custom_executors=tuple((name, executors[name]) for name in inventory)))


def followup(call: ToolCall, snapshot: TurnSnapshot, variant: Variant) -> str:
    name = ("browser_snapshot" if call.name == "browser_fill" and variant.fill_observe else
            "browser_read" if call.name == "browser_click" and variant.click_readback else "")
    specs = {spec.name: spec for spec in snapshot.frozen_specs}
    if (not name or snapshot.input_binding is not None or snapshot.browser.copy is not None
            or "codemode" not in snapshot.tool_names or call.name not in snapshot.tool_names
            or call.name not in snapshot.nested_tool_names or name not in snapshot.nested_tool_names
            or snapshot.policy is None or name not in specs
            or not snapshot.policy.allows(specs[name].grant)
            or snapshot.allowed is not None and name not in snapshot.allowed):
        return ""
    return name


def lower(plan: ToolPlan, snapshot: TurnSnapshot, variant: Variant) -> ToolPlan:
    """Only lower a fully admitted single action; JSON values remain inputs."""
    if plan.protocol_error or plan.control is not None and plan.control.kind != "continue" or len(plan.calls) != 1:
        return plan
    call = plan.calls[0]
    extra = followup(call, snapshot, variant)
    if not extra:
        return plan
    code = (f"const action=unwrap(await tools.{call.name}(inputs));"
            f"unwrap(await tools.{extra}({{}}));return action;")
    converted = ToolCall("codemode", {"code": code, "inputs": call.args}, call.call_id)
    # The generated execution input passes the original normalizer too.
    payload: JsonObject = {"tool": converted.name, "args": converted.args}
    checked = _ORIGINAL_NORMALIZE(decode_text_turn(json.dumps(payload, ensure_ascii=False)), snapshot=snapshot)
    if checked.protocol_error:
        raise AssertionError(f"generated program failed original admission: {checked.protocol_error}")
    return replace(plan, calls=[converted])


_ORIGINAL_NORMALIZE = kernel_protocol.normalize_turn


def children(result: ToolResult, session: TaskSession) -> list[tuple[str, ToolResult]]:
    refs = result.canonical.get("child_effect_ids")
    return [(ref, session._memory_results[ref]) for ref in refs
            if isinstance(ref, str) and ref in session._memory_results] if isinstance(refs, list) else []


def retain_browser_surface(snapshot: TurnSnapshot, _session: KernelSessionView, _name: str | None = None,
                           *, research: bool = False) -> TurnSnapshot:
    """Let each experimental arm own its offer, using the existing frozen authority."""
    return snapshot


def call_payload(call: ToolCall) -> JsonObject:
    payload: object = asdict(call)
    return decode_json_object(payload)


def variant_payload(variant: Variant) -> JsonObject:
    payload: object = asdict(variant)
    return canonical_json_object(payload)


@contextmanager
def intervention(directory: Path, variant: Variant) -> Generator[None, None, None]:
    build_original = turn_snapshot.build_turn_snapshot
    context_original = task_result_context
    freeze_original = KernelContext.freeze

    def build(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
        baseline = build_original(session, native=native)
        candidate = expose(baseline, session, variant)
        payload = snapshot_description(candidate)
        restored = restore_snapshot(payload)
        if restored != candidate or not restored.implementation_compatible:
            raise AssertionError("snapshot did not preserve the original execution contract")
        if (candidate.policy != baseline.policy or candidate.allowed != baseline.allowed
                or candidate.nested_tool_names != baseline.nested_tool_names
                or base.execution_specs(candidate) != base.execution_specs(baseline)
                or dict(candidate.custom_executors) != dict(baseline.custom_executors)):
            raise AssertionError("intervention broadened authority or changed tool definitions")
        append_json(directory / "snapshots.jsonl", {"baseline_tools": list(baseline.tool_names),
            "added": [name for name in candidate.tool_names if name not in baseline.tool_names], "snapshot": payload})
        return restored

    def normalize(reply: DecodedTextTurn | AssistantTurn, *, policy: TaskPolicy | None = None,
                  controller_allowed: Collection[str] | None = None, snapshot_names: tuple[str, ...] | None = None,
                  snapshot: TurnSnapshot | None = None) -> ToolPlan:
        plan = _ORIGINAL_NORMALIZE(reply, policy=policy, controller_allowed=controller_allowed,
                                   snapshot_names=snapshot_names, snapshot=snapshot)
        if snapshot is None:
            return plan
        lowered = lower(plan, snapshot, variant)
        if lowered != plan:
            append_json(directory / "lowering.jsonl", {"proposed": [call_payload(call) for call in plan.calls],
                "executed": [call_payload(call) for call in lowered.calls]})
        return lowered

    def context(session: TaskSession, result: ToolResult, scope: AnswerScope | None, *,
                tool_names: tuple[str, ...] | None = None) -> str:
        text = context_original(session, result, scope, tool_names=tool_names)
        rows = children(result, session)
        # No duplicated state: render the actual committed followup receipt.
        if (result.call.name == "codemode" and result.call.args.get("code") in {
                f"const action=unwrap(await tools.{action}(inputs));unwrap(await tools.{extra}({{}}));return action;"
                for action, extra in (("browser_fill", "browser_snapshot"), ("browser_click", "browser_read"))}
                and len(rows) >= 2):
            ref, child = rows[-1]
            text += (f"\n\nAutomatic followup, separately committed as {ref}. "
                     "The previous action was not undone; do not repeat it to recover output.\n"
                     + context_original(session, child, scope, tool_names=tool_names))
            if child.ok and child.call.name == "browser_snapshot":
                text += "\nUse refs from this new observation; earlier refs are obsolete."
        return text

    def freeze(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int,
               prompt: str = "", messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
               counters: RuntimeExecutionCounters | None = None, text_result_spans: tuple[TextResultSpan, ...] = (),
               coding_context_prepared: bool = False, initial_source_batch: bool = False, handoff: str | None = None,
               task_span: TaskPromptSpan | None = None) -> None:
        hints = []
        if variant.reference_guidance:
            hints.append(REFERENCE_GUIDANCE)
        if variant.fill_observe:
            hints.append("After a successful direct browser_fill, the runtime also obtains a fresh browser_snapshot "
                         "when authorized. Use the latest returned refs for the next action.")
        if variant.click_readback:
            hints.append("After a successful direct browser_click, the runtime also attempts browser_read when authorized. "
                         "Its separate receipt contains formal evidence if successful; inspect it before requesting another read.")
        if hints:
            prompt += "\n\n" + "\n".join(hints)
        freeze_original(context, state, snapshot, turn=turn, prompt=prompt, messages=messages, acknowledge=acknowledge,
                        counters=counters, text_result_spans=text_result_spans, coding_context_prepared=coding_context_prepared,
                        initial_source_batch=initial_source_batch, handoff=handoff, task_span=task_span)

    with (patch.object(turn_surface, "offer_browser_tools", retain_browser_surface),
          patch.object(turn_surface, "offer_browser_call", retain_browser_surface),
          patch.object(turn_snapshot, "build_turn_snapshot", build),
          patch.object(kernel_protocol, "normalize_turn", normalize),
          patch.object(kernel_context, "task_result_context", context),
          patch.object(KernelContext, "freeze", freeze)):
        yield


def jsonl(path: Path) -> list[JsonObject]:
    return [parse_json_object(line) for line in path.read_text(encoding="utf-8").splitlines()] \
        if path.exists() else []


def facts_present(text: str, facts: tuple[str, ...]) -> bool:
    return all(re.search(r"(?<![A-Za-z0-9_])" + re.escape(fact) + r"(?![A-Za-z0-9_])", text) if fact.isascii()
               else fact in text for fact in facts)


def phase_metrics(directory: Path) -> JsonObject:
    """Charge each actual response once, including failures and repeated done."""
    actions = base.response_actions(directory / "provider.jsonl")
    responses = [row for row in jsonl(directory / "provider.jsonl") if row.get("type") == "response"]
    if len(actions) != len(responses):
        raise AssertionError("response/action alignment failed")
    turns: dict[int, list[ToolResult]] = {}
    receipts = {row.identity: row.result for row in tool_receipts(directory / "state")}
    with sqlite3.connect((directory / "state/durable.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        intent_rows: list[tuple[object]] = db.execute("SELECT intent FROM effects WHERE category='tool_call' ORDER BY rowid").fetchall()
        for raw, in intent_rows:
            intent = parse_json_object(require_json_string(raw, "intent"))
            result = receipts.get(str(intent.get("effect_id")))
            if result is not None:
                turns.setdefault(int(str(intent["turn"])), []).append(result)
    totals: dict[str, JsonObject] = {}
    previous_stale = False
    for index, (action, response) in enumerate(zip(actions, responses, strict=True), 1):
        rows = turns.get(index, [])
        stale = any(not row.ok and isinstance(v := row.canonical.get("value"), dict)
                    and decode_json_object(v).get("reason") == "stale_ref" for row in rows)
        tool = action.get("tool")
        phase = "submission" if tool == "done" else "recovery" if stale or previous_stale else "operation"
        payload = decode_json_object(response["payload"])
        usage = decode_json_object(payload.get("usage", {}))
        row = totals.setdefault(phase, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})
        for key, value in (("calls", 1), ("prompt_tokens", usage.get("prompt_tokens")),
                           ("completion_tokens", usage.get("completion_tokens"))):
            if type(value) is not int:
                raise AssertionError(f"missing measured usage: {key}")
            row[key] = int(str(row[key])) + value
        row["seconds"] = round(float(str(row["seconds"])) + float(str(response["seconds"])), 3)
        previous_stale = stale
    return {"phases": decode_json_object(totals), "done_attempts": sum(a.get("tool") == "done" for a in actions)}


def assert_stable(source: str, harness: str) -> None:
    if production_source_digest() != source or harness_digest() != harness:
        raise RuntimeError("Experiment invalid: production or harness changed; stop and restart the WHOLE matrix.")


def run_variant(directory: Path, case: base.Case, name: str, target: Target, *, max_turns: int = 10) -> JsonObject:
    variant = VARIANTS[name]
    source, script = production_source_digest(), harness_digest()
    class GuardedProvider(ExperimentProvider):
        def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                                  response_bytes: int, seconds: float) -> None:
            if phase == "request":
                assert_stable(source, script)
            super()._observe_http_attempt(attempt=attempt, data=data, phase=phase,
                response_bytes=response_bytes, seconds=seconds)

    @contextmanager
    def configure(path: Path, _arm: str) -> Generator[None, None, None]:
        with intervention(path, variant):
            yield

    with patch.object(base, "experiment", configure), patch.object(base, "ExperimentProvider", GuardedProvider):
        row = base.run_arm(directory, case, "A", target, max_turns=max_turns)
    assert_stable(source, script)
    receipts = [item.result for item in tool_receipts(directory / "state")]
    evidence = decode_json_object(row["evidence"])
    formal = [r for r in receipts if r.ok and r.call.name in {"open_url", "browser_read"}
              and (body := _browser_body(r)) is not None and facts_present(body, case.facts)]
    facts = facts_present(str(row["answer"]), case.facts)
    operated = (all(any(r.ok and r.call.name == name for r in receipts) for name in case.required)
                and (not case.required or any(r.call.name == "browser_read" for r in formal))
                and (not case.field_value or decode_json_object(row["page_state"]).get("value") == case.field_value)
                and (not case.required or decode_json_object(row["page_state"]).get("clicks") == 1))
    row.update(variant=name, configuration=variant_payload(variant), harness_digest=script,
        facts_match=facts, operation_passed=operated,
        passed=bool(row["passed"]) and facts and bool(formal) and operated,
        formal_matching_reads=len(formal), automatic_programs=sum(r.call.name == "codemode" and
            str(r.call.args.get("code", "")).startswith("const action=unwrap(await tools.") for r in receipts),
        updated_source_cited=evidence["updated_source_cited"], **phase_metrics(directory))
    write_json(directory / "summary.json", row)
    return row


def audit_matrix(directory: Path, runs: list[JsonObject], manifest: JsonObject) -> JsonObject:
    expected = (len(decode_json_object(manifest["variants"])) * len(require_json_strings(manifest["cases"], "cases"))
                * len(require_json_strings(manifest["wordings"], "wordings")) * require_json_integer(manifest["repeats"], "repeats"))
    if len(runs) != expected:
        raise AssertionError("incomplete experiment matrix")
    configs: set[str] = set()
    paired: dict[tuple[str, str, str], list[Path]] = {}
    http_count = 0
    for run in runs:
        unit = directory / str(run["unit"])
        if not run["source_stable"] or not run["bound_before_model"]:
            raise AssertionError("unbound page or changing source")
        if run["source_digest"] != manifest["source_digest"] or run["harness_digest"] != manifest["harness_digest"]:
            raise AssertionError("mixed code versions")
        wire = [row for row in jsonl(unit / "provider.jsonl") if row.get("phase") == "request"]
        responses = [row for row in jsonl(unit / "provider.jsonl") if row.get("type") == "response"]
        if len(wire) != len(responses) or len(wire) != run["model_calls"]:
            raise AssertionError("hidden retries, transport failures or response count mismatch")
        for request in wire:
            if request.get("attempt") != 1:
                raise AssertionError("HTTP retry in the measured matrix")
            payload = decode_json_object(request["payload"])
            sampling: JsonObject = {key: payload.get(key) for key in ("model", "temperature", "seed", "stream")}
            configs.add(json.dumps(sampling, sort_keys=True))
        http_count += len(wire)
        if run["case"] != "visible":
            first = json.dumps(wire[0]["payload"], ensure_ascii=False)
            # Distinctive result codes cannot appear before the model operates.
            marker = base.CASES[str(run["case"])].facts[-1]
            if marker in first:
                raise AssertionError("expected result leaked into first request")
        phases = decode_json_object(run["phases"])
        for phase_metric, total_metric in (("calls", "model_calls"), ("prompt_tokens", "prompt_tokens")):
            if sum(int(str(decode_json_object(p)[phase_metric])) for p in phases.values()) != run[total_metric]:
                raise AssertionError("phase costs do not reconcile")
        paired.setdefault((str(run["case"]), str(run["wording"]), str(run["repeat"])), []).append(unit)
    for units in paired.values():
        inputs = {(unit / "input.json").read_bytes() for unit in units}
        authorities = set()
        for unit in units:
            snap = decode_json_object(jsonl(unit / "snapshots.jsonl")[0]["snapshot"])
            specs = snap.get("specs")
            if not isinstance(specs, list):
                raise AssertionError("missing frozen spec inventory")
            def spec_name(value: JsonValue) -> str:
                return str(decode_json_object(value)["name"])

            authority: JsonObject = {"policy": snap.get("policy"), "allowed": snap.get("allowed"),
                "nested": snap.get("nested_tool_names"),
                "specs": sorted(specs, key=spec_name)}
            authorities.add(json.dumps(authority, sort_keys=True))
        if len(inputs) != 1 or len(authorities) != 1:
            raise AssertionError("paired task, grants or execution contracts differ")
    target = decode_json_object(manifest["target"])
    expected_config: JsonObject = {"model": target["model"], "temperature": 0, "seed": target["seed"], "stream": False}
    expected_sampling = json.dumps(expected_config, sort_keys=True)
    if configs != {expected_sampling}:
        raise AssertionError("model sampling configuration differs")
    result: JsonObject = {"units": len(runs), "paired_groups": len(paired), "http_requests": http_count,
        "sampling": parse_json_object(next(iter(configs))), "same_inputs_and_authority": True,
        "no_initial_answer_leak": True, "no_hidden_retries": True, "phase_costs_reconcile": True}
    write_json(directory / "audit.json", result)
    return result


def aggregate(runs: list[JsonObject]) -> JsonObject:
    output: JsonObject = {}
    for name in {str(row["variant"]): None for row in runs}:
        rows = [row for row in runs if row["variant"] == name]
        summary: JsonObject = {"units": len(rows), "passed": sum(bool(row["passed"]) for row in rows),
            "interactive_units": sum(row["case"] != "visible" for row in rows),
            "interactive_operations_passed": sum(bool(row["operation_passed"]) and row["case"] != "visible" for row in rows)}
        for metric in ("model_calls", "prompt_tokens", "completion_tokens", "inference_seconds", "seconds",
                       "stale_ref_errors", "done_attempts", "javascript_proposals", "automatic_programs"):
            summary[metric] = round(sum(float(str(row[metric])) for row in rows), 3)
        counts: Counter[str] = Counter()
        for row in rows:
            for tool, count in decode_json_object(row["browser_calls"]).items():
                counts[tool] += require_json_integer(count, tool)
        summary["browser_calls"] = dict(counts)
        output[name] = summary
    return output


class Options(argparse.Namespace):
    run_dir: Path
    base_url: str
    variants: list[str]
    cases: list[str]
    wordings: list[str]
    repeats: int
    max_turns: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    default_variants: list[str] = list(VARIANTS)
    default_cases: list[str] = list(base.CASES)
    default_wordings: list[str] = ["original", "paraphrase"]
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=default_variants)
    parser.add_argument("--cases", nargs="+", choices=tuple(base.CASES), default=default_cases)
    parser.add_argument("--wordings", nargs="+", choices=("original", "paraphrase"), default=default_wordings)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args(namespace=Options())
    if args.repeats < 1 or args.max_turns < 1 or len(args.variants) != len(set(args.variants)):
        parser.error("positive budgets and distinct variants required")
    source, script = production_source_digest(), harness_digest()
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target_payload: object = asdict(target)
    manifest: JsonObject = {"source_digest": source, "harness_digest": script, "target": decode_json_object(target_payload),
        "variants": {name: variant_payload(VARIANTS[name]) for name in args.variants},
        "wordings": [wording for wording in args.wordings], "cases": [case for case in args.cases],
        "max_turns": args.max_turns, "repeats": args.repeats}
    write_json(args.run_dir / "manifest.json", manifest)
    runs: list[JsonObject] = []
    try:
        for repeat in range(args.repeats):
            for wi, wording in enumerate(args.wordings):
                for ci, case_name in enumerate(args.cases):
                    case = base.CASES[case_name]
                    if wording == "paraphrase":
                        case = replace(case, question=base.PARAPHRASES[case_name])
                    # Rotate and reverse arms so baseline is not always first.
                    offset = (ci + wi + repeat) % len(args.variants)
                    order = args.variants[offset:] + args.variants[:offset]
                    if (wi + repeat) % 2:
                        order.reverse()
                    for name in order:
                        assert_stable(source, script)
                        unit = f"{wording}-r{repeat + 1}-{case_name}-{name}"
                        print("Starting " + unit, flush=True)
                        row = run_variant(args.run_dir / unit, case, name, target, max_turns=args.max_turns)
                        assert_stable(source, script)
                        runs.append({"unit": unit, "case": case_name, "wording": wording, "repeat": repeat + 1, **row})
                        write_json(args.run_dir / "summary.json", {"valid": True, "complete": False,
                            "aggregate": aggregate(runs), "runs": runs})
                        print({key: row[key] for key in ("passed", "operation_passed", "model_calls", "stale_ref_errors")}, flush=True)
        assert_stable(source, script)
        audit_matrix(args.run_dir, runs, manifest)
    except Exception as exc:
        write_json(args.run_dir / "summary.json", {"valid": False, "complete": False,
            "error": f"{type(exc).__name__}: {exc}", "runs": runs})
        raise
    write_json(args.run_dir / "summary.json", {"valid": True, "complete": True, "aggregate": aggregate(runs), "runs": runs})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
