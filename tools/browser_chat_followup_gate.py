"""Run a paired browser Chat experiment through the existing Headless entry.

All candidate wiring is process-local. Defaults are four cumulative arms,
six Chromium fixtures and two phrasings, with no scripted model proposals.
The result guard covers one stable semantic result region in the same document.
This measures the candidate, including its completion gate, on those fixtures.
It does not test recovery of the candidate across a process restart.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codey.automation.browser_session import BrowserSession, _browser_call
from codey.codemode.kernel import CodeModeKernelExecutor
from codey.operations import completion_gate, kernel_context, kernel_protocol
from codey.operations.browser_copy import committed_children
from codey.operations.browser_upload import upload_wait_plan
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot, refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_guidance import task_result_context
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import _browser_body, require_tool_success
from codey.protocols.text_turn import DecodedTextTurn, TextRequest, TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn
from codey.research.passages import read_passage_refs
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, validate_args_with_spec
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_followup_ab as ab
from tools import browser_leaf_exposure_ab as base
from tools.browser_production_live_gate import schema_tool_names, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    Arm,
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)

ARMS = ("codemode", "direct", "fill_observe", "purpose")
PURPOSES = ("result", "controls", "action")
FILL = ab.VARIANTS["fill_observe"]
RESULT = "const action=unwrap(await tools.browser_click(inputs.click));"
READ = "unwrap(await tools.browser_read({}));return action;"
WAIT = "for(const wait of inputs.waits) unwrap(await tools.browser_wait(wait));"
CLICK_CONTROLS = RESULT + "unwrap(await tools.browser_snapshot({}));return action;"
RESULT_PROGRAMS = {RESULT + READ, RESULT + WAIT + READ}
FILL_PROGRAM = ("const action=unwrap(await tools.browser_fill(inputs));"
                "unwrap(await tools.browser_snapshot({}));return action;")
FOLLOWUP_PROGRAMS = {FILL_PROGRAM, CLICK_CONTROLS, *RESULT_PROGRAMS}
PENDING = "The click was delivered. Its result is still unconfirmed. Read the current result again; do not click again."
UNCERTAIN = "Click delivery is uncertain. Inspect the current page before taking another action."
WAIT_FOR_READY = {"condition": "selector", "value": '#record[data-ready="yes"]', "timeout_ms": 5000}


def async_case(*, delay: int = 1700, explicit_wait: bool = False) -> base.Case:
    required: tuple[str, ...] = ("browser_fill", "browser_click", "browser_read")
    if explicit_wait:
        required += (require_tool_success("browser_wait", arguments=WAIT_FOR_READY),)
    html = f'''<label>城市<input id="value"></label><button>应用</button>
<article id="record" data-ready="yes">上次查询，北京样本：净重 41 克，校验码 STALE_041。</article>
<script>
const field = document.getElementById("value"), record = document.getElementById("record");
function wire(button) {{
  button.onclick = () => {{
    window.clicks = (window.clicks || 0) + 1;
    const city = field.value;
    record.dataset.ready = "no";
    record.setAttribute("aria-busy", "true");
    setTimeout(() => {{
      if (city === "福州") record.textContent = "本次查询，福州样本：净重 263 克，校验码 FRESH_263。";
      record.dataset.ready = "yes";
      record.removeAttribute("aria-busy");
    }}, {delay});
  }};
}}
wire(document.querySelector("button"));
field.addEventListener("input", () => {{
  const old = document.querySelector("button"), fresh = old.cloneNode(true);
  old.replaceWith(fresh); wire(fresh);
}});
</script>'''
    return base.Case("把城市设为福州并查询，告诉我净重和校验码，附来源。", html,
                     ("福州", "263", "FRESH_263"), required, "福州")


CASES = {
    "visible": base.CASES["visible"],
    "click": replace(base.CASES["click"], question="帮我查一下 B-19 入库了没、放在哪儿，附来源。"),
    "quoted": replace(base.CASES["quoted"], question=f"备注请原样输入下面两行，再查询受理状态和回执码，附来源。\n{base.NOTE}"),
    "async": async_case(),
    "explicit_wait": async_case(explicit_wait=True),
    "controls": base.Case("先打开筛选菜单，再展开 C-7 的记录。告诉我负责人和回执码，附来源。", '''
<button id="menu">筛选菜单</button><button id="detail" hidden>展开 C-7</button>
<article id="record">档案查询。请选择记录，查看负责人和回执码。</article>
<script>
const menu = document.getElementById('menu'), detail = document.getElementById('detail');
menu.onclick = () => { window.clicks = (window.clicks || 0) + 1; detail.hidden = false; };
detail.onclick = () => { window.clicks = (window.clicks || 0) + 1;
  document.getElementById("record").textContent = "C-7 的负责人是周青，回执码 DETAIL_713。"; };
</script>''', ("周青", "DETAIL_713"), ("browser_click", "browser_read")),
}
PARAPHRASES = {
    "visible": base.PARAPHRASES["visible"],
    "click": "B-19 现在是什么状态、库位是哪里？请附上页面来源。",
    "quoted": f"请保留引号和换行录入这段备注，查它有没有受理，回执号是多少？请注明来源。\n{base.NOTE}",
    "async": "我要福州样本的重量和校验码。把城市切到福州，查一下并注明来源。",
    "explicit_wait": "我要福州样本的重量和校验码。把城市切到福州，查一下并注明来源。",
    "controls": "帮我从筛选菜单进入 C-7 的详情，查到经手人和回执编号，带上网页来源。",
}


def model_click_spec(spec: ToolSpec) -> ToolSpec:
    return freeze_spec_parameters(replace(spec, parameters=(*spec.parameters, (
        "purpose", {"type": "string", "enum": list(PURPOSES)})), required=(*spec.required, "purpose"),
        description=("Click a current observed ref. Choose purpose=result when the click queries or reveals the "
                     "content needed to answer the user; the runtime attempts a formal read. Choose controls to "
                     "open a menu, filter or form for another action; it returns new refs. Choose action when the "
                     "click itself is the requested outcome. A delivered click is not a reason to click again."),
        json_examples=('{"tool":"browser_click","args":{"ref":"<snapshot ref>","purpose":"result"}}',)))


def admits(snapshot: TurnSnapshot, name: str) -> bool:
    return (name in snapshot.nested_tool_names and (snapshot.allowed is None or name in snapshot.allowed)
            and snapshot.policy is not None
            and any(s.name == name and snapshot.policy.allows(s.grant) for s in snapshot.frozen_specs))


def normalize_candidate(reply: DecodedTextTurn | AssistantTurn, snapshot: TurnSnapshot, arm: str) -> ToolPlan:
    normalize = ab._ORIGINAL_NORMALIZE
    purpose = ""
    offered = arm == "purpose" and set(base.DIRECT_TOOLS) <= set(snapshot.tool_names)
    if offered:
        items = kernel_protocol._response_objects(reply)
        if isinstance(items, list) and any(name == "browser_click" for name, _, _ in items):
            if len(items) != 1:
                return kernel_protocol._invalid_plan("Submit one direct browser_click per turn.", tool="browser_click")
            _, args, _ = items[0]
            spec = next(s for s in snapshot.model_specs if s.name == "browser_click")
            if error := validate_args_with_spec(spec, args):
                return kernel_protocol._invalid_plan(error, tool="browser_click")
            purpose = str(args["purpose"])
            stripped = {key: value for key, value in args.items() if key != "purpose"}
            if isinstance(reply, TextRequests):
                reply = replace(reply, items=(replace(reply.items[0], args=stripped),))
            elif isinstance(reply, AssistantTurn) and reply.tool_calls:
                reply = replace(reply, tool_calls=(replace(reply.tool_calls[0], arguments=stripped),))
            else:
                reply = TextRequests((TextRequest("browser_click", stripped),))
    plan = normalize(reply, snapshot=snapshot)
    if arm in {"fill_observe", "purpose"}:
        plan = ab.lower(plan, snapshot, FILL)
    if not purpose or purpose == "action" or plan.protocol_error:
        return plan
    call = plan.calls[0]
    assert call.name == "browser_click"
    extra = "browser_snapshot" if purpose == "controls" else "browser_read"
    if not admits(snapshot, extra):
        return plan
    inputs: JsonObject = {"click": call.args}
    code = CLICK_CONTROLS
    if purpose == "result":
        assert snapshot.policy is not None
        waits = upload_wait_plan(snapshot.policy.required_checks)
        if waits.missing or (waits.waits and not admits(snapshot, "browser_wait")):
            return kernel_protocol._invalid_plan("The declared wait cannot be run within this tool contract.", tool=call.name)
        inputs["waits"] = list(waits.waits)
        code = RESULT + (WAIT if waits.waits else "") + READ
    args = {"code": code, "inputs": inputs}
    checked = normalize(decode_text_turn(json.dumps({"tool": "codemode", "args": args}, ensure_ascii=False)),
                        snapshot=snapshot)
    if checked.protocol_error:
        raise AssertionError(checked.protocol_error)
    return replace(plan, calls=[replace(checked.calls[0], call_id=call.call_id)])


# Use visible semantic result regions. The candidate never receives the fixture's
# expected answer, expected region selector or expected update time.
REGIONS = """() => [...document.querySelectorAll('article, [role=status], [role=region], [aria-live]')]
  .filter(el => el.getClientRects().length && getComputedStyle(el).visibility === 'visible')
  .map((el, i) => ({key: el.id || `${el.tagName}:${i}`, text: el.innerText.replace(/\\s+/g, ' ').trim(),
                  busy: !!el.closest('[aria-busy=true]')}))"""


def observe_regions(browser: BrowserSession) -> JsonObject:
    doc = browser._document_identity_on_browser_thread()
    if doc is None:
        return {}
    page = browser._page_on_browser_thread()
    rows = page.evaluate(REGIONS)
    return decode_json_object({"document": doc, "regions": rows})


def changed_region(before: JsonObject, after: JsonObject) -> str:
    if not before.get("document") or before["document"] != after.get("document"):
        return ""
    old, new = before.get("regions"), after.get("regions")
    if not isinstance(old, list) or len(old) != 1 or not isinstance(new, list) or len(new) != 1:
        return ""
    previous, current = old[0], new[0]
    if (isinstance(previous, dict) and isinstance(current, dict) and previous.get("key") == current.get("key")
            and current.get("busy") is False and isinstance(body := current.get("text"), str)
            and body != previous.get("text")):
        return body
    return ""


def pending_click(session: TaskSession) -> tuple[str, ToolResult] | None:
    confirmed: set[str] = set()
    for ref, receipt in reversed(tuple(session._memory_results.items())):
        if receipt.ok and receipt.call.name == "browser_read" and isinstance(click := receipt.audit.get("query_click"), str):
            confirmed.add(click)
        value = receipt.canonical.get("value")
        if (receipt.call.name == "browser_click" and "query_baseline" in receipt.audit
                and isinstance(value, dict) and value.get("delivery") in {"delivered", "uncertain"}
                and ref not in confirmed):
            return ref, receipt
    return None


def guard_capture(browser: BrowserSession, baseline: JsonObject, *, wait_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + wait_seconds
    while True:
        cancellation.check()
        current = observe_regions(browser)
        if current.get("document") != baseline.get("document"):
            raise RuntimeError(PENDING + " The bound document changed.")
        region = changed_region(baseline, current)
        if region:
            capture = browser._capture_on_browser_thread()
            body = capture.get("text")
            if (capture.get("status") == "ok" and isinstance(body, str)
                    and region in " ".join(body.split()) and observe_regions(browser) == current):
                return capture
        if time.monotonic() >= deadline:
            raise RuntimeError(PENDING)
        cancellation.wait(0.1)


@contextmanager
def intervention(directory: Path, arm: str) -> Iterator[None]:
    original_snapshot = KernelContext.request_snapshot
    original_specs = TurnSnapshot.model_specs.fget  # type: ignore[attr-defined]
    original_freeze = KernelContext.freeze
    original_execute = ExecutionDelegate._execute_browser_operation
    original_capture = BrowserSession.capture
    original_parent = CodeModeKernelExecutor.execute
    original_gate = completion_gate.evaluate
    active: dict[str, str] = {}
    generated: set[str] = set()

    def model_specs(snapshot: TurnSnapshot) -> tuple[ToolSpec, ...]:
        specs: tuple[ToolSpec, ...] = original_specs(snapshot)
        if arm == "purpose" and set(base.DIRECT_TOOLS) <= set(snapshot.tool_names):
            return tuple(model_click_spec(spec) if spec.name == "browser_click" else spec for spec in specs)
        return specs

    def request_snapshot(context: KernelContext, snapshot: TurnSnapshot) -> tuple[TurnSnapshot, str]:
        baseline, handoff = original_snapshot(context, snapshot)
        candidate = baseline
        research = context.execution.research_tools
        browser = research.browser_session if research is not None else None
        if arm != "codemode" and not handoff and not context.session.policy.strict_research and browser is not None:
            offered = base.expose_leaves(baseline, context.session)
            if set(base.DIRECT_TOOLS) <= set(offered.tool_names) and browser.document_identity() is not None:
                candidate = offered
        candidate = refresh_snapshot_contract(candidate)
        payload = snapshot_description(candidate)
        restored = restore_snapshot(payload)
        if (restored != candidate or not restored.implementation_compatible or restored.policy != baseline.policy
                or restored.allowed != baseline.allowed or restored.nested_tool_names != baseline.nested_tool_names
                or {s.name: s for s in restored.frozen_specs} != {s.name: s for s in baseline.frozen_specs}
                or dict(restored.custom_executors) != dict(baseline.custom_executors)):
            raise AssertionError("Candidate changed frozen execution authority or failed snapshot round trip")
        append_json(directory / "snapshots.jsonl", {"baseline_tools": list(baseline.tool_names), "snapshot": payload})
        return restored, handoff

    def normalize(reply: DecodedTextTurn | AssistantTurn, **kwargs: Any) -> ToolPlan:
        snapshot = kwargs.get("snapshot")
        if not isinstance(snapshot, TurnSnapshot):
            return ab._ORIGINAL_NORMALIZE(reply, **kwargs)
        plan = normalize_candidate(reply, snapshot, arm)
        for call in plan.calls:
            if call.name != "codemode" or str(call.args.get("code")) not in FOLLOWUP_PROGRAMS:
                continue
            items = kernel_protocol._response_objects(reply)
            if isinstance(items, list) and len(items) == 1 and items[0][0] in {"browser_fill", "browser_click"}:
                generated.add(call_key(call))
                append_json(directory / "lowering.jsonl", {"proposed": items[0][0], "executed": asdict(call)})
        return plan

    def freeze(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, **kwargs: Any) -> None:
        if arm in {"fill_observe", "purpose"} and "browser_fill" in snapshot.tool_names:
            kwargs["prompt"] = str(kwargs.get("prompt", "")) + (
                "\nA successful direct browser_fill automatically obtains a new browser_snapshot. Use its newest refs.")
        original_freeze(context, state, snapshot, **kwargs)

    def run_parent(executor: CodeModeKernelExecutor, call: ToolCall) -> ToolResult:
        code = str(call.args.get("code"))
        if arm != "purpose" or code not in RESULT_PROGRAMS or call_key(call) not in generated:
            return original_parent(executor, call)
        active[executor.execution_id] = code
        try:
            return original_parent(executor, call)
        finally:
            active.pop(executor.execution_id, None)

    def execute(delegate: ExecutionDelegate, browser: BrowserSession, call: ToolCall, *, turn: int,
                tool_index: int) -> tuple[ToolResult, bool, int | None]:
        parent = call.call_id.rpartition(":child:")[0]
        session = delegate.session
        if call.name == "browser_click" and parent in active:
            before = _browser_call(observe_regions, browser)
            result, ok, code = original_execute(delegate, browser, call, turn=turn, tool_index=tool_index)
            return replace(result, audit={**result.audit, "query_baseline": before}), ok, code
        if call.name != "browser_read" or session is None or (pending := pending_click(session)) is None:
            return original_execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        ref, click = pending
        value = decode_json_object(click.canonical["value"])
        if value.get("delivery") == "uncertain":
            return ToolResult(call, UNCERTAIN, ok=False), False, None
        baseline = decode_json_object(click.audit["query_baseline"])
        expected_waits = upload_wait_plan(session.policy.required_checks).waits
        if expected_waits:
            from codey.operations.browser_completion import committed_results

            # All required waits must have settled after this click, on its document.
            ids = tuple(session._memory_results)
            suffix = set(ids[ids.index(ref) + 1:])
            waits = [row for key, row in committed_results(session.executed, session._memory_results,
                     frozenset({"browser_wait"})) if key in suffix
                     and row.audit.get("query_wait_document") == baseline.get("document")]
            if any(not any(all(row.call.args.get(k) == v for k, v in expected.items()) for row in waits)
                   for expected in expected_waits):
                return ToolResult(call, PENDING + " Complete the declared wait first.", ok=False), False, None

        def capture(resource: BrowserSession) -> dict[str, object]:
            if resource is not browser:
                return original_capture(resource)
            return _browser_call(guard_capture, resource, baseline, wait_seconds=2.5)

        with patch.object(BrowserSession, "capture", capture):
            result, ok, code = original_execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        if ok:
            result = replace(result, audit={**result.audit, "query_click": ref})
        return result, ok, code

    def execute_with_wait(delegate: ExecutionDelegate, browser: BrowserSession, call: ToolCall, *, turn: int,
                          tool_index: int) -> tuple[ToolResult, bool, int | None]:
        if arm != "purpose" or call.name != "browser_wait" or delegate.session is None or pending_click(delegate.session) is None:
            return execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        before = browser.document_identity()
        result, ok, code = original_execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        if ok and before is not None and browser.document_identity() == before:
            result = replace(result, audit={**result.audit, "query_wait_document": before})
        return result, ok, code

    def context(session: TaskSession, result: ToolResult, scope: Any, **kwargs: Any) -> str:
        rendered = task_result_context(session, result, scope, **kwargs)
        code = str(result.call.args.get("code"))
        if result.call.name != "codemode" or code not in FOLLOWUP_PROGRAMS or call_key(result.call) not in generated:
            return rendered
        committed = committed_children(session._memory_results)
        children = [(ref, child) for ref, child in ab.children(result, session) if ref in committed]
        if not children:
            return rendered
        first = children[0][1]
        value = first.canonical.get("value")
        delivery = value.get("delivery") if isinstance(value, dict) else None
        if delivery in {"delivered", "verified"}:
            rendered += "\nThe action was delivered. Recover missing output without repeating the action."
        elif delivery == "uncertain":
            rendered += "\nThe action's delivery is uncertain. Inspect the page before taking another action."
        for ref, child in children[1:]:
            rendered += f"\n\nAutomatic followup {child.call.name} ({ref}):\n" + task_result_context(session, child, scope, **kwargs)
            if child.ok and child.call.name == "browser_snapshot":
                rendered += "\nUse these new refs for the next action."
        return rendered

    def evaluate(session: TaskSession, done_text: object, **kwargs: Any) -> completion_gate.GateVerdict:
        if arm == "purpose" and (pending := pending_click(session)) is not None:
            value = decode_json_object(pending[1].canonical["value"])
            uncertain = value.get("delivery") == "uncertain"
            append_json(directory / "pending-submissions.jsonl", {
                "reason": "click_delivery_uncertain" if uncertain else "query_result_unconfirmed", "click": pending[0]})
            return completion_gate.GateVerdict(False, UNCERTAIN if uncertain else PENDING, None)
        return original_gate(session, done_text, **kwargs)

    with (patch.object(TurnSnapshot, "model_specs", property(model_specs)),
          patch.object(KernelContext, "request_snapshot", request_snapshot),
          patch.object(KernelContext, "freeze", freeze), patch.object(kernel_protocol, "normalize_turn", normalize),
          patch.object(kernel_context, "task_result_context", context),
          patch.object(CodeModeKernelExecutor, "execute", run_parent),
          patch.object(ExecutionDelegate, "_execute_browser_operation", execute_with_wait),
          patch.object(completion_gate, "evaluate", evaluate)):
        yield


def call_key(call: ToolCall) -> str:
    return json.dumps(asdict(call), sort_keys=True, ensure_ascii=False)


def harness_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes() + ab.harness_digest().encode()
                          + Path(ab.__file__).read_bytes()).hexdigest()


def assert_stable(source: str, harness: str) -> None:
    if production_source_digest() != source or harness_digest() != harness:
        raise RuntimeError("Production or harness changed during the run. Discard this matrix and start a new one.")


def score(directory: Path, name: str, case: base.Case, row: JsonObject) -> JsonObject:
    receipts = tool_receipts(directory / "state")
    expected_clicks = 0 if name == "visible" else 2 if name == "controls" else 1
    delivered = [(i, item) for i, item in enumerate(receipts) if item.result.call.name == "browser_click"
                 and isinstance(value := item.result.canonical.get("value"), dict) and value.get("delivery") == "delivered"]
    state = decode_json_object(row["page_state"])
    clicks_match = state.get("clicks") == len(delivered) == expected_clicks
    field_matches = not case.field_value or state.get("value") == case.field_value
    last_click = delivered[-1][0] if delivered else -1
    last_click_ref = delivered[-1][1].identity if delivered else ""
    reads = [item for item in receipts[last_click + 1:] if item.result.ok and
             item.result.call.name == ("browser_read" if expected_clicks else "open_url")]
    answer = str(row["answer"])
    matching = [item for item in reads if (body := _browser_body(item.result)) is not None
                and ab.facts_present(body, case.facts) and "STALE_041" not in body]
    citations = set(re.findall(r"\[([^\[\]\s]+)\]", answer))
    exact = [item for item in matching if citations.intersection(read_passage_refs(item.result.canonical["read_passages"]))]
    cited = [item for item in matching if item in exact or item.result.canonical.get("source_id") in citations]
    waits = [item for item in receipts[last_click + 1:] if item.result.ok and item.result.call.name == "browser_wait"
             and item.result.call.args == WAIT_FOR_READY]
    wait_order = name != "explicit_wait" or bool(waits and matching and receipts.index(waits[0]) < receipts.index(matching[0]))
    generated = {json.dumps(item["executed"], sort_keys=True, ensure_ascii=False)
                 for item in ab.jsonl(directory / "lowering.jsonl")}
    actions = [item.result for item in receipts if item.result.call.name == "codemode"
               and call_key(item.result.call) in generated]
    verified_fills = [item for item in receipts[:last_click] if item.result.call.name == "browser_fill"
                      and item.result.ok and item.result.call.args.get("value") == case.field_value]
    operation_passed = (clicks_match and field_matches and bool(matching) and wait_order
                        and (not case.field_value or len(verified_fills) == 1))
    return {**row, "operation_passed": operation_passed,
            "passed": row["stop_reason"] == "done" and operation_passed and bool(cited)
                      and ab.facts_present(answer, case.facts) and "STALE_041" not in answer,
            "actual_clicks": state.get("clicks"), "expected_clicks": expected_clicks,
            "last_click_receipt": last_click_ref, "matching_read_receipts": [item.identity for item in matching],
            "cited_matching_reads": [item.identity for item in cited],
            "exact_passage_citations": [item.identity for item in exact], "wait_order": wait_order,
            "stale_body_reads": sum("STALE_041" in item.result.model_text for item in reads),
            "stale_answer": "STALE_041" in answer,
            "query_linked_reads": sum(item.result.audit.get("query_click") == last_click_ref for item in matching),
            "fill_followups": sum(item.call.args.get("code") == FILL_PROGRAM for item in actions),
            "controls_followups": sum(item.call.args.get("code") == CLICK_CONTROLS for item in actions),
            "result_followups": sum(item.call.args.get("code") in RESULT_PROGRAMS for item in actions),
            "pending_submissions": len(ab.jsonl(directory / "pending-submissions.jsonl")), **ab.phase_metrics(directory)}


def run_unit(directory: Path, name: str, case: base.Case, arm: str, target: Target, *, max_turns: int = 10,
             source: str | None = None, harness: str | None = None) -> JsonObject:
    expected_source = source or production_source_digest()
    expected_harness = harness or harness_digest()

    class GuardedProvider(ExperimentProvider):
        def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                                  response_bytes: int, seconds: float) -> None:
            if phase == "request":
                assert_stable(expected_source, expected_harness)
            super()._observe_http_attempt(attempt=attempt, data=data, phase=phase,
                response_bytes=response_bytes, seconds=seconds)

    @contextmanager
    def configure(path: Path, _arm: Arm) -> Iterator[None]:
        with intervention(path, arm):
            yield

    assert_stable(expected_source, expected_harness)
    with patch.object(base, "experiment", configure), patch.object(base, "ExperimentProvider", GuardedProvider):
        original = base.run_arm(directory, case, "A", target, max_turns=max_turns)
    assert_stable(expected_source, expected_harness)
    row = score(directory, name, case, original)
    row.update(unit=directory.name, case=name, arm=arm, variant=arm, harness_digest=expected_harness,
               fixture_digest=hashlib.sha256(case.html.encode()).hexdigest())
    write_json(directory / "summary.json", row)
    return row


def authority(snapshot: JsonObject) -> str:
    specs, executors = snapshot["specs"], snapshot["executors"]
    if not isinstance(specs, list) or not isinstance(executors, list):
        raise AssertionError("Missing frozen definitions or executors")
    return json.dumps({"policy": snapshot["policy"], "allowed": snapshot["allowed"],
        "nested": snapshot["nested_tool_names"],
        "specs": sorted(specs, key=lambda row: str(decode_json_object(row)["name"])),
        "executors": sorted(executors, key=lambda row: str(decode_json_object(row)["name"]))}, sort_keys=True)


def audit_matrix(directory: Path, runs: list[JsonObject], *, target: Target, arms: list[str],
                 cases: list[str], wordings: list[str], repeats: int, source: str, harness: str) -> JsonObject:
    expected = {(name, wording, repeat, arm) for name in cases for wording in wordings
                for repeat in range(1, repeats + 1) for arm in arms}
    seen: set[tuple[object, ...]] = set()
    pairs: dict[tuple[object, ...], set[tuple[bytes, str, str]]] = {}
    requests = 0
    for run in runs:
        identity = (run["case"], run["wording"], run["repeat"], run["variant"])
        if identity not in expected or identity in seen:
            raise AssertionError("Unexpected or duplicate experiment cell")
        seen.add(identity)
        unit = directory / str(run["unit"])
        if (run["source_digest"] != source or run["harness_digest"] != harness
                or not run["source_stable"] or not run["bound_before_model"]):
            raise AssertionError("Unbound page or mixed source versions")
        snapshots = ab.jsonl(unit / "snapshots.jsonl")
        snapshot = decode_json_object(snapshots[0]["snapshot"])
        pairs.setdefault(identity[:3], set()).add(((unit / "input.json").read_bytes(),
                                                  str(run["fixture_digest"]), authority(snapshot)))
        provider = ab.jsonl(unit / "provider.jsonl")
        sent = [event for event in provider if event.get("phase") == "request"]
        received = [event for event in provider if event.get("type") == "response"]
        if not sent or len(sent) != len(received) or len(sent) != run["model_calls"]:
            raise AssertionError("Model request and response counts differ")
        sampling: JsonObject = {"model": target.model, "seed": target.seed, "temperature": 0,
                                "stream": False, "max_tokens": 8192}
        for request in sent:
            payload = decode_json_object(request["payload"])
            if request.get("attempt") != 1 or any(payload.get(k) != v for k, v in sampling.items()):
                raise AssertionError("HTTP retry or mismatched model configuration")
        first = decode_json_object(sent[0]["payload"])
        case = CASES[str(run["case"])]
        if case.required and case.facts[-1] in json.dumps(first, ensure_ascii=False):
            raise AssertionError("Expected answer leaked into first model request")
        schema = decode_json_object(first["response_format"])
        visible = schema_tool_names(schema) & set(base.DIRECT_TOOLS)
        if visible != (set() if run["variant"] == "codemode" else set(base.DIRECT_TOOLS)):
            raise AssertionError("First model request does not expose the arm's expected tools")
        if (run["variant"] == "purpose") != ('"purpose"' in json.dumps(schema)):
            raise AssertionError("Click purpose declaration differs from the arm")
        phases = decode_json_object(run["phases"])
        for metric, total in (("calls", "model_calls"), ("prompt_tokens", "prompt_tokens")):
            if sum(int(str(decode_json_object(phase)[metric])) for phase in phases.values()) != run[total]:
                raise AssertionError("Phase counts do not reconcile")
        requests += len(sent)
    if seen != expected or any(len(values) != 1 for values in pairs.values()):
        raise AssertionError("Incomplete matrix or different task, fixture, or authority in a pair")
    return {"cells": len(seen), "paired_groups": len(pairs), "http_requests": requests,
            "same_paired_inputs_fixture_and_authority": True, "first_request_schema_verified": True,
            "no_initial_answer_leak": True, "no_hidden_retries": True, "phase_costs_reconcile": True}


METRICS = ("model_calls", "prompt_tokens", "stale_ref_errors", "done_attempts", "stale_body_reads",
           "fill_followups", "controls_followups", "result_followups", "query_linked_reads", "pending_submissions")


def summarize(runs: list[JsonObject]) -> JsonObject:
    result: JsonObject = {}
    for arm in ARMS:
        rows = [row for row in runs if row["variant"] == arm]
        if rows:
            result[arm] = {"cells": len(rows), "passed": sum(row["passed"] is True for row in rows),
                           "stale_answers": sum(row["stale_answer"] is True for row in rows),
                           **{key: sum(int(str(row[key])) for row in rows) for key in METRICS}}
    return result


def paired_comparisons(runs: list[JsonObject]) -> list[JsonValue]:
    """Compare cost on matched successes as well as completion over all cells."""
    grouped = {(str(row["case"]), str(row["wording"]), str(row["repeat"]), str(row["variant"])): row for row in runs}
    comparisons: list[JsonValue] = []
    for before, after in zip(ARMS, ARMS[1:], strict=False):
        pairs = [(row, other) for (case, wording, repeat, arm), row in grouped.items() if arm == before
                 and (other := grouped.get((case, wording, repeat, after))) is not None]
        if not pairs:
            continue
        common = [(a, b) for a, b in pairs if a["passed"] is True and b["passed"] is True]
        comparisons.append({"before": before, "after": after, "paired_cells": len(pairs),
            "gained": sum(a["passed"] is False and b["passed"] is True for a, b in pairs),
            "lost": sum(a["passed"] is True and b["passed"] is False for a, b in pairs),
            "common_successes": len(common),
            "common_success_cost": {key: [sum(int(str(a[key])) for a, _ in common),
                                         sum(int(str(b[key])) for _, b in common)]
                                    for key in ("model_calls", "prompt_tokens")}})
    return comparisons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=list(CASES))
    parser.add_argument("--wordings", nargs="+", choices=("original", "paraphrase"), default=["original", "paraphrase"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args()
    if args.repeats < 1 or args.max_turns < 1 or any(len(values) != len(set(values)) for values in
            (args.arms, args.cases, args.wordings)):
        parser.error("Use positive budgets and distinct arms, cases and wordings")
    source, harness = production_source_digest(), harness_digest()
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    write_json(args.run_dir / "manifest.json", {"source_digest": source, "harness_digest": harness, "target": asdict(target),
        "arms": args.arms, "cases": args.cases, "wordings": args.wordings, "max_turns": args.max_turns,
        "repeats": args.repeats})
    runs: list[JsonObject] = []
    try:
        for repeat in range(1, args.repeats + 1):
            for wi, wording in enumerate(args.wordings):
                for ci, name in enumerate(args.cases):
                    case = CASES[name]
                    if wording == "paraphrase":
                        case = replace(case, question=PARAPHRASES[name])
                    offset = (ci + wi + repeat - 1) % len(args.arms)
                    order = args.arms[offset:] + args.arms[:offset]
                    if (wi + repeat) % 2 == 0:
                        order.reverse()
                    for arm in order:
                        unit = f"{wording}-r{repeat}-{name}-{arm}"
                        print(f"Starting {unit}", flush=True)
                        row = run_unit(args.run_dir / unit, name, case, arm, target, max_turns=args.max_turns,
                                       source=source, harness=harness)
                        runs.append({**row, "wording": wording, "repeat": repeat})
                        print({key: row[key] for key in ("passed", "model_calls", "actual_clicks", "query_linked_reads")}, flush=True)
                        write_json(args.run_dir / "summary.json", {"valid": False, "complete": False, "runs": runs})
        assert_stable(source, harness)
        audit = audit_matrix(args.run_dir, runs, target=target, arms=args.arms, cases=args.cases, wordings=args.wordings,
                             repeats=args.repeats, source=source, harness=harness)
    except (Exception, KeyboardInterrupt) as exc:
        write_json(args.run_dir / "summary.json", {"valid": False, "complete": False,
            "error": f"{type(exc).__name__}: {exc}", "runs": runs})
        raise
    write_json(args.run_dir / "audit.json", audit)
    write_json(args.run_dir / "summary.json", {"valid": True, "complete": True, "aggregate": summarize(runs),
        "paired_comparisons": paired_comparisons(runs), "runs": runs})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
