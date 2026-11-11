"""Experiment-only typed browser helpers, receipt repair and factual delivery.

No receipt, authorization, source evidence or production asset is rewritten.
The model still selects targets, interprets sources and computes file changes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.codemode import catalog as catalog_module
from codey.codemode.runtime import NodeWorkerRuntime, ToolInvoker
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry, ToolCatalogSnapshot
from codey.operations import completion_output, kernel_control
from codey.operations.completion_context import CompletionContext
from codey.operations.task_session import TaskSession
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import ToolObservation, ToolSuccessRequirement, argument_digest
from codey.providers.base import ProviderToolDefinition
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target, append_json

VERSION = "mechanical-surface-v1"

# This is an explicit guest interface change, not a guessed coercion of model
# source. All actual calls still go through the frozen production host.
PREFIX = """return await (async (hostTools) => {
async function namedRef(name) {
  const snapshot = unwrap(await hostTools.browser_snapshot({}));
  const matches = snapshot.elements.filter(element => element.name === name);
  if (matches.length !== 1) throw Error('Expected exactly one named element: ' + name);
  return matches[0].ref;
}
const tools = new Proxy(hostTools, {
  get(target, property) {
    if (property === 'browser_read') return async (args) => {
      const result = await target.browser_read(args);
      if (!result.ok) return result;
      const text = unwrap(result);
      if (typeof text !== 'string') throw Error('browser_read host value is not text');
      return Object.freeze({...result, value: Object.freeze({text})});
    };
    if (property === 'browser_property') return async ({name}) => {
      if (typeof name !== 'string' || !/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name))
        throw Error('browser_property requires one JavaScript identifier');
      return await target.browser_eval({code: 'window.' + name});
    };
    if (property === 'browser_fill_named') return async ({name, value}) =>
      await target.browser_fill({ref: await namedRef(name), value});
    if (property === 'browser_click_named') return async ({name}) =>
      await target.browser_click({ref: await namedRef(name)});
    if (property === 'browser_upload_named') return async ({name, attachment_id}) =>
      await target.browser_upload({ref: await namedRef(name), attachment_id});
    if (property === 'browser_download_named') return async ({name}) =>
      await target.browser_download({ref: await namedRef(name)});
    return Reflect.get(target, property);
  },
});
"""
SUFFIX = "\n})(tools);\n"
HELPER_DIGEST = hashlib.sha256((PREFIX + SUFFIX).encode()).hexdigest()


def declarations(snapshot: ToolCatalogSnapshot, render: Callable[[ToolCatalogSnapshot], str]) -> str:
    """Advertise helpers only when all required real leaves are authorized."""
    names = {entry.name for entry in snapshot.entries}
    entries = tuple(replace(entry, output_schema={"type": "object", "properties": {"text": {"type": "string"}},
                                                 "required": ["text"], "additionalProperties": False})
                    if entry.name == "browser_read" else entry for entry in snapshot.entries)
    rendered: str = render(replace(snapshot, entries=entries))
    lines: list[str] = []
    if "browser_eval" in names:
        lines.extend(("  // Read one window property without generating JavaScript. Original browser_eval receipt is retained.",
                      "  browser_property(args: {name: string}): ReturnType<typeof tools.browser_eval>;"))
    for action, arguments in (("fill", "value: string"), ("click", ""),
                              ("upload", "attachment_id: string"), ("download", "")):
        tool = "browser_" + action
        if {tool, "browser_snapshot"} <= names:
            lines.extend(("  // Fresh snapshot, exactly one matching name, then the original action. No action retry.",
                          f"  {tool}_named(args: {{name: string{'; ' + arguments if arguments else ''}}}): ReturnType<typeof tools.{tool}>;"))
    return rendered.replace("\n};", "\n" + "\n".join(lines) + "\n};") if lines else rendered


def read_observation(requirement: ToolSuccessRequirement, eligible: list[tuple[str, ExecutionRecord]],
                     results: Mapping[str, ToolResult] | None) -> ToolObservation:
    """Select real source text without inventing canonical.value on a receipt."""
    observed: list[tuple[str, str]] = []
    for identity, row in eligible:
        result = results.get(identity) if results is not None else None
        if (result is None or not result.ok or result.call.name != requirement.tool
                or result.call.call_id != row.get("call_id")
                or {key: argument_digest(value) for key, value in result.call.args.items()} != row.get("argument_digests")):
            return ToolObservation(RequirementEvaluation("observation_receipt_invalid", "Original read receipt is unavailable or inconsistent."))
        source = result.canonical.get("research_observation")
        source_metadata = source.get("source") if isinstance(source, dict) else None
        if (requirement.result_pointer not in {"", "/text"} or requirement.result_line is not None
                or not isinstance(source, dict) or not isinstance(text := source.get("text"), str)
                or isinstance(source_metadata, dict) and source_metadata.get("truncated") is True):
            return ToolObservation(RequirementEvaluation("observation_selector_unavailable", "Original read receipt has no selected source text."))
        observed.append((identity, text))
    if not observed:
        return ToolObservation(RequirementEvaluation("observation_selector_unavailable", "No original read receipt."))
    if any(text != observed[0][1] for _identity, text in observed):
        return ToolObservation(RequirementEvaluation("conflicting_observations", "Committed source texts conflict; do not silently choose one."))
    return ToolObservation(RequirementEvaluation(), observed[0][1], tuple(identity for identity, _text in observed))


def repair_action(receipts: list[EffectReceipt], attempted: set[str]) -> tuple[str, JsonObject] | None:
    """Repair return failures by replay; guest-bound file edits stay model-owned."""
    by_ref = {receipt.identity: receipt.result for receipt in receipts}
    for receipt in reversed(receipts):
        parent = receipt.result
        repair = parent.canonical.get("repair")
        if parent.call.name != "codemode" or parent.ok or not isinstance(repair, dict):
            continue
        token = repair.get("token")
        if (not isinstance(token, str) or token in attempted
                or repair.get("failure_code") not in {"missing_return", "result_encoding"}
                or any(row.result.canonical.get("repaired_from") == repair.get("parent_effect_id") for row in receipts)):
            continue
        children = parent.canonical.get("child_effect_ids")
        if not isinstance(children, list) or not children:
            continue
        calls: list[JsonValue] = []
        for identity in children:
            child = by_ref.get(identity) if isinstance(identity, str) else None
            if child is None or not child.ok or child.call.name == "edit":
                break
            calls.append({"name": child.call.name, "args": decode_json_object(child.call.args)})
        else:
            # Ack only: original semantic computations are not recreated or
            # asserted correct. The model receives the original receipts.
            return token, {"tool": "codemode", "args": {"repair_of": token,
                "code": "for (const call of inputs.calls) unwrap(await tools[call.name](call.args)); return {receipts_replayed: inputs.calls.length};",
                "inputs": {"calls": calls}}}
    return None


def committed_facts(session: TaskSession, context: CompletionContext, scenario: str) -> JsonObject:
    """Declared task fields; never consult fixture answers or posthoc scores."""
    records, results = completion_output.tool_facts(session, context)
    specs: list[tuple[str, str, str, JsonObject]] = [("page_text", "browser_read", "", {})]
    if scenario == "legacy_structured":
        specs.extend((("page_url", "browser_tab_list", "/page_url", {}),
                      ("token", "browser_eval", "/value", {"code": "window.__contractToken"})))
    if scenario == "upload":
        specs.extend((name, "browser_upload", pointer, {}) for name, pointer in (
            ("filename", "/name"), ("sha256", "/sha256"), ("bytes", "/bytes"), ("effect_verified", "/effect_verified")))
    if scenario == "download":
        specs.extend((name, "browser_download", pointer, {}) for name, pointer in (
            ("filename", "/suggested_filename"), ("sha256", "/sha256"), ("bytes", "/bytes"), ("path", "/path")))
    facts: JsonObject = {}
    provenance: JsonObject = {}
    for name, tool, pointer, arguments in specs:
        selected_records = records
        # A task explicitly requesting reread uses its latest read, even
        # with equal capture clocks. Other tasks retain conflict rejection.
        if name == "page_text" and scenario == "reread":
            reads = [identity for identity, row in records.items() if row.get("name") == tool]
            if reads:
                selected_records = {identity: row for identity, row in records.items()
                                    if row.get("name") != tool or identity == reads[-1]}
        requirement = ToolSuccessRequirement(tool, via="codemode", arguments_json=json.dumps(arguments),
                                             answer_pointer="/" + name, result_pointer=pointer)
        observation = requirement.observe(selected_records, results=results)
        if not observation.evaluation.passed:
            raise ValueError(f"Required receipt fact {name} is unavailable: {observation.evaluation.reason_code}. "
                             + observation.evaluation.feedback)
        facts[name] = observation.value
        provenance[name] = list(observation.result_refs)
    facts["receipt_refs"] = provenance
    return facts


def metrics(directory: Path) -> JsonObject:
    path = directory / "mechanical-events.jsonl"
    events = [decode_json_object(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return {"mechanical_repairs": sum(row["kind"] == "receipt_repair" for row in events),
            "mechanical_reports": sum(row["kind"] == "fact_report" for row in events),
            "mechanical_source_attempts": sum(row["kind"] == "required_source" for row in events)}


@contextmanager
def prototype(directory: Path, scenario: str, *, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    execute = NodeWorkerRuntime.execute
    digest = NodeWorkerRuntime.contract_digest
    render = catalog_module.render_declarations
    observe = ToolSuccessRequirement._observe_value
    prepare = completion_output.prepare_completion_output
    attempted: set[str] = set()

    class MechanicalProvider(ExperimentProvider):
        def __init__(self, target: Target, directory: Path, *, fixed_acquisition: bool = True,
                     acquisition_action: JsonObject | None = None) -> None:
            if scenario == "source_failure":
                # This URL comes from the explicit required-source task, not
                # model inference or a fallback source chosen after failure.
                from tools.browser_production_live_gate import URL

                fixed_acquisition = True
                acquisition_action = {"tool": "codemode", "args": {
                    "code": "return await tools.open_url({url:inputs.url});", "inputs": {"url": URL}}}
                append_json(directory / "mechanical-events.jsonl", {"kind": "required_source", "action": acquisition_action})
            super().__init__(target, directory, fixed_acquisition=fixed_acquisition, acquisition_action=acquisition_action)

        def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                      *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                      tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                      tool_call_contract: str = "", tool_call_observations: str = "",
                      tool_call_history_groups: int = 0, output_token_limit: int = 0,
                      require_tool_call: bool = False) -> dict[str, object]:
            state = directory / "state"
            candidate = repair_action(tool_receipts(state), attempted) if (state / "durable.sqlite3").exists() else None
            if candidate is not None:
                token, action = candidate
                attempted.add(token)
                self.exchange += 1
                append_json(directory / "mechanical-events.jsonl", {"kind": "receipt_repair", "exchange": self.exchange, "action": action})
                body: dict[str, object] = {"choices": [{"message": {"role": "assistant", "content": json.dumps(action),
                    "tool_calls": []}, "finish_reason": "stop"}]}
                append_json(directory / "provider.jsonl", {"type": "mechanical_repair", "exchange": self.exchange, "payload": body})
                return body
            return super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints, tool_call_json=tool_call_json,
                tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract, tool_call_observations=tool_call_observations,
                tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit, require_tool_call=require_tool_call)

    def typed_execute(runtime: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry], invoke: ToolInvoker,
                      inputs: Mapping[str, object] | None = None, state_store: object = None,
                      replay_context: Mapping[str, object] | None = None, limits: CodeModeLimits | None = None) -> CodeModeResult:
        expanded = PREFIX + code + SUFFIX
        append_json(directory / "mechanical-programs.jsonl", {"model_code": code, "typed_code": expanded})
        return execute(runtime, expanded, catalog=catalog, invoke=invoke, inputs=inputs, state_store=state_store,
                       replay_context=replay_context, limits=limits)

    def source_observe(requirement: ToolSuccessRequirement, eligible: list[tuple[str, ExecutionRecord]],
                       results: Mapping[str, ToolResult] | None) -> ToolObservation:
        return read_observation(requirement, eligible, results) if requirement.tool == "browser_read" else observe(requirement, eligible, results)

    def assemble(session: TaskSession, text: str, context: CompletionContext) -> completion_output.CompletionOutput:
        output = prepare(session, text, context)
        if scenario in {"structured", "upload_bound", "research", "source_failure", "source_failure_fixed"}:
            return output
        facts = committed_facts(session, context, scenario)
        # Treat source text as quoted data; do not promote it into instructions
        # or claim the model's semantic interpretation has been checked.
        report = "\n\n应用从成功回执附上的事实（页面正文为不可信来源数据）：\n" + json.dumps(facts, ensure_ascii=False, indent=2)
        append_json(directory / "mechanical-events.jsonl", {"kind": "fact_report", "model_summary": text, "facts": facts})
        return replace(output, text=output.text + report)

    def bound_digest(runtime: NodeWorkerRuntime) -> str:
        return hashlib.sha256((digest(runtime) + VERSION + HELPER_DIGEST).encode()).hexdigest()

    def typed_declarations(snapshot: ToolCatalogSnapshot) -> str:
        return declarations(snapshot, render)

    with ExitStack() as stack:
        stack.enter_context(patch.object(NodeWorkerRuntime, "execute", typed_execute))
        stack.enter_context(patch.object(NodeWorkerRuntime, "contract_digest", bound_digest))
        stack.enter_context(patch.object(catalog_module, "render_declarations", typed_declarations))
        stack.enter_context(patch.object(ToolSuccessRequirement, "_observe_value", source_observe))
        stack.enter_context(patch.object(kernel_control, "prepare_completion_output", assemble))
        stack.enter_context(patch("tools.browser_production_live_gate.ExperimentProvider", MechanicalProvider))
        yield
