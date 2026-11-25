"""Run a paired browser Chat experiment through the existing Headless entry.

All candidate wiring is process-local. Defaults include an arm for frozen
verbatim fills, with no scripted model proposals.
The result guard covers one stable semantic result region in the same document.
This measures the candidate, including its completion gate, on those fixtures.
It does not test recovery of the candidate across a process restart.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_session import BrowserSession
from codey.codemode.kernel import CodeModeKernelExecutor
from codey.operations import completion_gate, provider_receipts
from codey.operations.browser import turn_surface
from codey.operations.browser.browser_copy import committed_children
from codey.operations.browser.browser_text_report import BrowserMetadata, MetadataPending
from codey.operations.browser.browser_waits import wait_plan as upload_wait_plan
from codey.operations.browser.execution import BrowserExecutor
from codey.operations.kernel import kernel_context, kernel_protocol, turn_presentation
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.kernel_tool_args import _invalid_plan
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_guidance import task_result_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import ToolObservation, _browser_body, require_tool_success
from codey.protocols.text_turn import DecodedTextTurn, TextRequest, TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolResult
from codey.providers.text_result_views import TextResultSpan
from codey.research import provenance
from codey.research.answer_contract import AnswerScope
from codey.research.browser_result_evidence import _confirmed_region_passages
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import restore_ledger_observation
from codey.research.passages import read_passage_refs
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, validate_args_with_spec
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_string
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

ARMS = ("codemode", "direct", "fill_observe", "purpose", "bound_text")
PURPOSE_ARMS = {"purpose", "bound_text"}
BOUND_SOURCE = "task_text"
MAX_BOUND_TEXT = 4096
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
    "quoted_block": replace(base.CASES["quoted"], question=(
        f"请把代码块里的原文填入备注框，再查询受理状态和回执码，附来源。\n```text\n{base.NOTE}\n```")),
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
    "quoted_block": f"录入以下代码块原文作为备注。它受理了吗？回执号是多少？请注明来源。\n```\n{base.NOTE}\n```",
    "async": "我要福州样本的重量和校验码。把城市切到福州，查一下并注明来源。",
    "explicit_wait": "我要福州样本的重量和校验码。把城市切到福州，查一下并注明来源。",
    "controls": "帮我从筛选菜单进入 C-7 的详情，查到经手人和回执编号，带上网页来源。",
}


def explicit_fill_text(task: str) -> str | None:
    """Extract a single explicit payload from the user task, never the fixture."""
    newline = re.search(r"\r?\n", task)
    if newline is None:
        return None
    instruction, tail = task[:newline.start()], task[newline.end():]
    if not re.search(r"填|录入|输入", instruction) or re.search(r"不要|禁止|无需|不用|别填|别输入|别录入", instruction):
        return None
    literal = "备注" in instruction and any(word in instruction for word in ("原文", "原样"))
    if literal and "代码块" in instruction:
        match = re.fullmatch(r"```(?:text)?\r?\n(?P<body>[\s\S]+?)\r?\n```(?:\r?\n)?", tail)
        if match is None:
            return None
        body: object = match.group("body")
        value = require_json_string(body, "body")
        if re.search(r"(?m)^\s*```", value):
            return None
    elif literal and re.search(r"(?:以下|下面)(?:两|2)行", instruction):
        if not re.fullmatch(r"[^\r\n]+\r?\n[^\r\n]+", tail):
            return None
        value = tail
    elif re.search(r"保留(?:双)?引号和换行(?:填入|录入|输入)这段备注", instruction):
        # The request explicitly points to one following text block. Preserve
        # its lines as data even when one of them reads like an instruction.
        lines = tail.splitlines()
        if len(lines) < 2 or any(not line.strip() or line.lstrip().startswith("```") for line in lines):
            return None
        value = tail
    else:
        return None
    return value if value.strip() and len(value) <= MAX_BOUND_TEXT else None


@dataclass(frozen=True)
class BoundFillSnapshot(TurnSnapshot):
    fill_text: str = ""


_restoring_fill_text: ContextVar[str] = ContextVar("restoring_fill_text", default="")


def with_fill_text(snapshot: TurnSnapshot, value: str) -> BoundFillSnapshot:
    bound = BoundFillSnapshot(snapshot.allowed, snapshot.contract_text)
    attributes: dict[str, object] = vars(snapshot)
    for name, attribute in attributes.items():
        object.__setattr__(bound, name, attribute)
    object.__setattr__(bound, "fill_text", value)
    return bound


def model_fill_spec(spec: ToolSpec) -> ToolSpec:
    return freeze_spec_parameters(replace(spec, parameters=(
        *(parameter for parameter in spec.parameters if parameter[0] != "value"),
        ("source", {"type": "string", "enum": [BOUND_SOURCE]})),
        required=("ref", "source"),
        description=("Fill the observed field with the original text explicitly bound from the user's request. "
                     "Select source=task_text. The runtime copies the original text verbatim, including newlines."),
        json_examples=('{"tool":"browser_fill","args":{"ref":"<snapshot ref>","source":"task_text"}}',)))


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


def can_bind_fill(snapshot: TurnSnapshot) -> bool:
    return (snapshot.input_binding is None and snapshot.browser.copy is None and snapshot.research.scope is None
            and snapshot.policy is not None and not snapshot.policy.strict_research
            and {"codemode", *base.DIRECT_TOOLS} <= set(snapshot.tool_names)
            and all(admits(snapshot, name) for name in base.DIRECT_TOOLS))


def normalize_candidate(reply: DecodedTextTurn | AssistantTurn, snapshot: TurnSnapshot, arm: str) -> ToolPlan:
    normalize = ab._ORIGINAL_NORMALIZE
    purpose = ""
    offered = arm in PURPOSE_ARMS and set(base.DIRECT_TOOLS) <= set(snapshot.tool_names)
    if offered:
        items = kernel_protocol._response_objects(reply)
        adapted = {"browser_click"}
        if isinstance(snapshot, BoundFillSnapshot):
            adapted.add("browser_fill")
        if isinstance(items, list) and any(name in adapted for name, _, _ in items):
            if len(items) != 1:
                return _invalid_plan("Submit one direct browser action per turn.")
            name, args, _ = items[0]
            spec = next(s for s in snapshot.model_specs if s.name == name)
            if error := validate_args_with_spec(spec, args):
                return _invalid_plan(error, tool=name)
            if name == "browser_fill":
                if not isinstance(snapshot, BoundFillSnapshot) or not can_bind_fill(snapshot):
                    return _invalid_plan("No authorized frozen fill text.", tool=name)
                stripped: JsonObject = {"ref": args["ref"], "value": snapshot.fill_text}
            else:
                purpose = str(args["purpose"])
                stripped = {key: value for key, value in args.items() if key != "purpose"}
            if isinstance(reply, TextRequests):
                reply = replace(reply, items=(replace(reply.items[0], args=stripped),))
            elif isinstance(reply, AssistantTurn) and reply.tool_calls:
                reply = replace(reply, tool_calls=(replace(reply.tool_calls[0], arguments=stripped),))
            else:
                reply = TextRequests((TextRequest(name, stripped),))
    plan = normalize(reply, snapshot=snapshot)
    if arm == "fill_observe" or arm in PURPOSE_ARMS:
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
            return _invalid_plan("The declared wait cannot be run within this tool contract.", tool=call.name)
        inputs["waits"] = list(waits.waits)
        code = RESULT + (WAIT if waits.waits else "") + READ
    lowered_args: JsonObject = {"code": code, "inputs": inputs}
    lowered: JsonObject = {"tool": "codemode", "args": lowered_args}
    checked = normalize(decode_text_turn(json.dumps(lowered, ensure_ascii=False)),
                        snapshot=snapshot)
    if checked.protocol_error:
        raise AssertionError(checked.protocol_error)
    return replace(plan, calls=[replace(checked.calls[0], call_id=call.call_id)])


# Use visible semantic result regions. The candidate never receives the fixture's
# expected answer, expected region selector or expected update time.
REGIONS = """() => {
  const busy = el => {
    for (let node = el; node; node = node.parentElement)
      if (node.getAttribute('aria-busy') === 'true') return true;
    return false;
  };
  return [...document.querySelectorAll('article, [role=status], [role=region], [aria-live]')]
  .filter(el => (el.offsetWidth || el.offsetHeight) && getComputedStyle(el).visibility === 'visible')
  .map((el, i) => ({key: el.id || `${el.tagName}:${i}`, text: el.innerText.replace(/\\s+/g, ' ').trim(),
                  busy: busy(el)}));
}"""


def observe_regions(browser: BrowserSession) -> JsonObject:
    doc = browser.document_identity()
    if doc is None:
        return {}
    rows = browser.inspect_state("(" + REGIONS + ")()")
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
            capture = browser.capture()
            body = capture.get("text")
            if (capture.get("status") == "ok" and isinstance(body, str)
                    and region in " ".join(body.split()) and observe_regions(browser) == current):
                return {**capture, "query_confirmed": True,
                        "query_region": {"text": region, "visible": True}}
        if time.monotonic() >= deadline:
            raise RuntimeError(PENDING)
        cancellation.wait(0.1)


@contextmanager
def intervention(directory: Path, arm: str) -> Generator[None, None, None]:
    original_snapshot = KernelContext.request_snapshot
    original_specs = turn_presentation.model_tool_specs
    original_freeze = KernelContext.freeze
    original_execute = BrowserExecutor.execute_operation
    original_query_capture = BrowserSession.capture_query_result
    original_parent = CodeModeKernelExecutor.execute
    original_gate = completion_gate.evaluate
    active: dict[str, str] = {}
    generated: set[str] = set()

    def model_specs(snapshot: TurnSnapshot) -> tuple[ToolSpec, ...]:
        frozen = original_specs(snapshot)
        if arm in PURPOSE_ARMS and set(base.DIRECT_TOOLS) <= set(snapshot.tool_names):
            bound = isinstance(snapshot, BoundFillSnapshot) or bool(_restoring_fill_text.get())
            return tuple(model_click_spec(spec) if spec.name == "browser_click" else
                         model_fill_spec(spec) if bound and spec.name == "browser_fill" else spec
                         for spec in frozen)
        return frozen

    def describe(snapshot: TurnSnapshot) -> JsonObject:
        payload = snapshot_description(snapshot)
        if isinstance(snapshot, BoundFillSnapshot):
            payload["experiment_fill_binding"] = {"version": 1, "source": BOUND_SOURCE, "text": snapshot.fill_text}
        return payload

    def restore(payload: object) -> TurnSnapshot:
        row = dict(decode_json_object(payload))
        if "experiment_fill_binding" not in row:
            return restore_snapshot(row)
        binding = decode_json_object(row.pop("experiment_fill_binding"))
        value = binding.get("text")
        if (arm != "bound_text" or set(binding) != {"version", "source", "text"}
                or type(binding["version"]) is not int or binding["version"] != 1 or binding["source"] != BOUND_SOURCE
                or not isinstance(value, str) or not value.strip() or len(value) > MAX_BOUND_TEXT):
            raise ValueError("Invalid frozen fill binding")
        token = _restoring_fill_text.set(value)
        try:
            snapshot = restore_snapshot(row)
        finally:
            _restoring_fill_text.reset(token)
        if not can_bind_fill(snapshot):
            raise ValueError("Frozen fill binding lacks its authorized tool contract")
        return with_fill_text(snapshot, value)

    def request_snapshot(context: KernelContext, snapshot: TurnSnapshot, *,
                         delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        baseline, handoff = original_snapshot(context, snapshot, delivered_results=delivered_results)
        candidate = replace(baseline, browser=replace(baseline.browser, followups=False, fill_text=None))
        research = context.execution.research_tools
        browser = research.browser_session if research is not None else None
        # Receipt progress now produces handoff text on ordinary browser turns too.
        # Exposure still intersects the frozen nested tools, policy and allowlist.
        if arm != "codemode" and not context.session.policy.strict_research and browser is not None:
            offered = base.expose_leaves(candidate, context.session)
            if set(base.DIRECT_TOOLS) <= set(offered.tool_names) and browser.document_identity() is not None:
                candidate = offered
        if arm == "bound_text" and can_bind_fill(candidate) and (value := explicit_fill_text(context.session.task_text)):
            candidate = with_fill_text(candidate, value)
        candidate = refresh_snapshot_contract(candidate)
        payload = describe(candidate)
        restored = restore(payload)
        if (restored != candidate or not restored.implementation_compatible or restored.policy != baseline.policy
                or restored.allowed != baseline.allowed or restored.nested_tool_names != baseline.nested_tool_names
                or base.execution_specs(restored) != base.execution_specs(baseline)
                or dict(restored.custom_executors) != dict(baseline.custom_executors)):
            raise AssertionError("Candidate changed frozen execution authority or failed snapshot round trip")
        append_json(directory / "snapshots.jsonl", {"baseline_tools": list(baseline.tool_names), "snapshot": payload})
        return restored, handoff

    def normalize(reply: DecodedTextTurn | AssistantTurn, *, policy: TaskPolicy | None = None,
                  controller_allowed: Collection[str] | None = None, snapshot_names: tuple[str, ...] | None = None,
                  snapshot: TurnSnapshot | None = None) -> ToolPlan:
        if snapshot is None or (policy is not None and policy != snapshot.policy):
            return ab._ORIGINAL_NORMALIZE(reply, policy=policy, controller_allowed=controller_allowed,
                                          snapshot_names=snapshot_names, snapshot=snapshot)
        plan = normalize_candidate(reply, snapshot, arm)
        for call in plan.calls:
            if call.name != "codemode" or str(call.args.get("code")) not in FOLLOWUP_PROGRAMS:
                continue
            items = kernel_protocol._response_objects(reply)
            if isinstance(items, list) and len(items) == 1 and items[0][0] in {"browser_fill", "browser_click"}:
                generated.add(call_key(call))
                executed: object = asdict(call)
                append_json(directory / "lowering.jsonl", {"proposed": items[0][0], "executed": decode_json_object(executed),
                    "bound_fill": isinstance(snapshot, BoundFillSnapshot) and items[0][0] == "browser_fill"})
        return plan

    def freeze(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int,
               prompt: str = "", messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
               counters: RuntimeExecutionCounters | None = None, text_result_spans: tuple[TextResultSpan, ...] = (),
               coding_context_prepared: bool = False, initial_source_batch: bool = False, handoff: str | None = None) -> None:
        if (arm == "fill_observe" or arm in PURPOSE_ARMS) and "browser_fill" in snapshot.tool_names:
            prompt += (
                "\nA successful direct browser_fill automatically obtains a new browser_snapshot. Use its newest refs.")
        original_freeze(context, state, snapshot, turn=turn, prompt=prompt, messages=messages, acknowledge=acknowledge,
                        counters=counters, text_result_spans=text_result_spans, coding_context_prepared=coding_context_prepared,
                        initial_source_batch=initial_source_batch, handoff=handoff)

    def run_parent(executor: CodeModeKernelExecutor, call: ToolCall) -> ToolResult:
        code = str(call.args.get("code"))
        if arm not in PURPOSE_ARMS or code not in RESULT_PROGRAMS or call_key(call) not in generated:
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
            before = observe_regions(browser)
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
            from codey.automation.browser_receipts import committed_results

            # All required waits must have settled after this click, on its document.
            ids = tuple(session._memory_results)
            suffix = set(ids[ids.index(ref) + 1:])
            waits = [row for key, row in committed_results(session.executed, session._memory_results,
                     frozenset({"browser_wait"})) if key in suffix
                     and row.audit.get("query_wait_document") == baseline.get("document")]
            if any(not any(all(row.call.args.get(k) == v for k, v in expected.items()) for row in waits)
                   for expected in expected_waits):
                return ToolResult(call, PENDING + " Complete the declared wait first.", ok=False), False, None

        def capture(resource: BrowserSession, original: JsonObject, *, pending: str) -> dict[str, object]:
            if resource is not browser:
                return original_query_capture(resource, original, pending=pending)
            try:
                return guard_capture(resource, baseline, wait_seconds=2.5)
            except RuntimeError as exc:
                raise QueryObservationError("query_update_unconfirmed", str(exc)) from exc

        with patch.object(BrowserSession, "capture_query_result", capture):
            result, ok, code = original_execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        if ok:
            result = replace(result, audit={**result.audit, "query_click": ref})
        return result, ok, code

    def execute_with_wait(delegate: ExecutionDelegate, browser: BrowserSession, call: ToolCall, *, turn: int,
                          tool_index: int) -> tuple[ToolResult, bool, int | None]:
        if arm not in PURPOSE_ARMS or call.name != "browser_wait" or delegate.session is None or pending_click(delegate.session) is None:
            return execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        before = browser.document_identity()
        result, ok, code = original_execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        if ok and before is not None and browser.document_identity() == before:
            result = replace(result, audit={**result.audit, "query_wait_document": before})
        return result, ok, code

    def context(session: TaskSession, result: ToolResult, scope: AnswerScope | None, *,
                tool_names: tuple[str, ...] | None = None) -> str:
        rendered = task_result_context(session, result, scope, tool_names=tool_names)
        code = str(result.call.args.get("code"))
        if result.call.name != "codemode" or code not in FOLLOWUP_PROGRAMS or call_key(result.call) not in generated:
            return rendered
        committed = committed_children(session._memory_results)
        children = [(ref, child) for ref, child in ab.children(result, session) if ref in committed]
        if not children:
            return rendered
        first = children[0][1]
        value = first.canonical.get("value")
        delivery = decode_json_object(value).get("delivery") if isinstance(value, dict) else None
        if delivery in {"delivered", "verified"}:
            rendered += "\nThe action was delivered. Recover missing output without repeating the action."
        elif delivery == "uncertain":
            rendered += "\nThe action's delivery is uncertain. Inspect the page before taking another action."
        for ref, child in children[1:]:
            rendered += (f"\n\nAutomatic followup {child.call.name} ({ref}):\n"
                         + task_result_context(session, child, scope, tool_names=tool_names))
            if child.ok and child.call.name == "browser_snapshot":
                rendered += "\nUse these new refs for the next action."
        return rendered

    def evaluate(session: TaskSession, done_text: object, *, context: object = None,
                 observations: Mapping[str, ToolObservation] | None = None,
                 metadata: BrowserMetadata | MetadataPending | None = None) -> completion_gate.GateVerdict:
        if arm in PURPOSE_ARMS and (pending := pending_click(session)) is not None:
            value = decode_json_object(pending[1].canonical["value"])
            uncertain = value.get("delivery") == "uncertain"
            append_json(directory / "pending-submissions.jsonl", {
                "reason": "click_delivery_uncertain" if uncertain else "query_result_unconfirmed", "click": pending[0]})
            return completion_gate.GateVerdict(False, UNCERTAIN if uncertain else PENDING, None)
        return original_gate(session, done_text, context=context, observations=observations, metadata=metadata)

    # Each arm offers only its own browser surface. The production offer is
    # already enabled on ordinary Chat, so it must stay out of this experiment.
    with (patch.object(turn_surface, "offer_browser_tools", ab.retain_browser_surface),
          patch.object(turn_surface, "offer_browser_call", ab.retain_browser_surface),
          patch.object(turn_presentation, "model_tool_specs", model_specs),
          patch.object(provider_receipts, "snapshot_description", describe),
          patch.object(provider_receipts, "restore_snapshot", restore),
          patch.object(KernelContext, "request_snapshot", request_snapshot),
          patch.object(KernelContext, "freeze", freeze), patch.object(kernel_protocol, "normalize_turn", normalize),
          patch.object(kernel_context, "task_result_context", context),
          patch.object(CodeModeKernelExecutor, "execute", run_parent),
          patch.object(BrowserExecutor, "execute_operation", execute_with_wait),
          patch.object(completion_gate, "evaluate", evaluate)):
        yield


def call_key(call: ToolCall) -> str:
    payload: object = asdict(call)
    return json.dumps(decode_json_object(payload), sort_keys=True, ensure_ascii=False)


def harness_digest() -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes() + ab.harness_digest().encode()
                            + Path(ab.__file__).read_bytes())
    # The shared production hash covers tracked files. Also guard new Python
    # modules imported from a working tree before they have been staged.
    untracked = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard", "--", "codey"],
                               check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    for path in sorted(name for name in untracked if name.endswith(".py")):
        digest.update(path.encode() + b"\0" + Path(path).read_bytes())
    return digest.hexdigest()


def assert_stable(source: str, harness: str) -> None:
    if production_source_digest() != source or harness_digest() != harness:
        raise RuntimeError("Production or harness changed during the run. Discard this matrix and start a new one.")


def _scored_browser_body(result: ToolResult, click: str) -> str | None:
    body = _browser_body(result)
    if body is not None:
        return body
    document, region = result.audit.get("browser_document"), result.audit.get("query_region")
    if isinstance(document, dict) and isinstance(url := document.get("url"), str) and _confirmed_region_passages(result, click, url):
        return require_json_string(decode_json_object(region)["text"], "queried region")
    return None


def score(directory: Path, name: str, case: base.Case, row: JsonObject) -> JsonObject:
    receipts = tool_receipts(directory / "state")
    expected_clicks = 0 if name == "visible" else 2 if name == "controls" else 1
    delivered = [(i, item) for i, item in enumerate(receipts) if item.result.call.name == "browser_click"
                 and isinstance(value := item.result.canonical.get("value"), dict)
                 and decode_json_object(value).get("delivery") == "delivered"]
    state = decode_json_object(row["page_state"])
    clicks_match = state.get("clicks") == len(delivered) == expected_clicks
    field_matches = not case.field_value or state.get("value") == case.field_value
    last_click = delivered[-1][0] if delivered else -1
    last_click_ref = delivered[-1][1].identity if delivered else ""
    reads = [item for item in receipts[last_click + 1:] if item.result.ok and
             item.result.call.name == ("browser_read" if expected_clicks else "open_url")]
    answer = str(row["answer"])
    matching = [item for item in reads if (body := _scored_browser_body(item.result, last_click_ref)) is not None
                and ab.facts_present(body, case.facts) and "STALE_041" not in body]
    citations = {match.group()[1:-1] for match in re.finditer(r"\[[^\[\]\s]+\]", answer)}
    exact = [item for item in matching if citations.intersection(read_passage_refs(item.result.canonical["read_passages"]))]
    cited = [item for item in matching if item in exact or item.result.canonical.get("source_id") in citations]
    waits = [item for item in receipts[last_click + 1:] if item.result.ok and item.result.call.name == "browser_wait"
             and item.result.call.args == WAIT_FOR_READY]
    wait_order = name != "explicit_wait" or bool(waits and matching and receipts.index(waits[0]) < receipts.index(matching[0]))
    generated = {json.dumps(item["executed"], sort_keys=True, ensure_ascii=False)
                 for item in ab.jsonl(directory / "lowering.jsonl")}
    bound = {json.dumps(item["executed"], sort_keys=True, ensure_ascii=False)
             for item in ab.jsonl(directory / "lowering.jsonl") if item.get("bound_fill") is True}
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
            "field_matches": field_matches, "verified_exact_fills": len(verified_fills),
            "last_click_receipt": last_click_ref, "matching_read_receipts": [item.identity for item in matching],
            "cited_matching_reads": [item.identity for item in cited],
            "exact_passage_citations": [item.identity for item in exact], "wait_order": wait_order,
            "stale_body_reads": sum("STALE_041" in item.result.model_text for item in reads),
            "stale_answer": "STALE_041" in answer,
            "query_linked_reads": sum(item.result.audit.get("query_click") == last_click_ref for item in matching),
            "fill_followups": sum(item.call.args.get("code") == FILL_PROGRAM for item in actions),
            "bound_fills": sum(item.ok and call_key(item.call) in bound for item in actions),
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
    def configure(path: Path, _arm: Arm) -> Generator[None, None, None]:
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

    def name(row: JsonValue) -> str:
        return str(decode_json_object(row)["name"])

    payload: JsonObject = {"policy": snapshot["policy"], "allowed": snapshot["allowed"],
        "nested": snapshot["nested_tool_names"],
        "specs": sorted([{k: v for k, v in decode_json_object(row).items() if k != "declarations" or name(row) != "codemode"} for row in specs], key=name),
        "executors": sorted(executors, key=name)}
    return json.dumps(payload, sort_keys=True)


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
        if any(decode_json_object(row["snapshot"]).get("browser_fill_text") is not None
               or decode_json_object(row["snapshot"]).get("browser_followups") is True for row in snapshots):
            raise AssertionError("Production browser follow-ups leaked into an experimental arm")
        pair = (run["case"], run["wording"], run["repeat"])
        pairs.setdefault(pair, set()).add(((unit / "input.json").read_bytes(),
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
        if (str(run["variant"]) in PURPOSE_ARMS) != ('"purpose"' in json.dumps(schema)):
            raise AssertionError("Click purpose declaration differs from the arm")
        task = parse_json_object((unit / "input.json").read_text(encoding="utf-8"))["task"]
        if not isinstance(task, str):
            raise AssertionError("Missing paired task text")
        expected_text = explicit_fill_text(task) if run["variant"] == "bound_text" else None
        actual_binding = snapshot.get("experiment_fill_binding")
        if expected_text is None:
            if actual_binding is not None or BOUND_SOURCE in json.dumps(schema):
                raise AssertionError("Fill binding exposed without explicit source boundaries")
        elif (actual_binding != {"version": 1, "source": BOUND_SOURCE, "text": expected_text}
              or BOUND_SOURCE not in json.dumps(schema)):
            raise AssertionError("Model schema or frozen payload differs from the explicitly bound fill")
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
           "fill_followups", "bound_fills", "controls_followups", "result_followups", "query_linked_reads", "pending_submissions")


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
    grouped: dict[tuple[str, str, str, str], JsonObject] = {
        (str(row["case"]), str(row["wording"]), str(row["repeat"]), str(row["variant"])): row for row in runs}
    comparisons: list[JsonValue] = []
    for before, after in pairwise(ARMS):
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


def citation_boundary_replay(directory: Path) -> list[JsonObject]:
    """Replay the citation check on raw done drafts and receipts from the run."""
    receipts = tool_receipts(directory / "state")
    ledger = ResearchLedger()
    source_ids: dict[str, str] = {}
    for item in receipts:
        result = item.result
        if not result.ok or not isinstance(observation := result.canonical.get("research_observation"), dict):
            continue
        restore_ledger_observation(ledger, decode_json_object(observation))
        if isinstance(label := result.canonical.get("source_id"), str):
            source = decode_json_object(decode_json_object(observation)["source"])
            if isinstance(url := source.get("final_url"), str):
                source_ids[label] = url
    original = provenance._urls_in_text

    def check(summary: str) -> JsonObject:
        def legacy_scan(text: str, _opened_url: Callable[[str], str] | None = None) -> list[str]:
            return original(text)

        with patch.object(provenance, "_urls_in_text", legacy_scan):
            before = provenance.citation_problem(summary, ledger=ledger, source_ids=source_ids, require_citation=False)
        replacements: JsonObject = {}

        def scan(text: str, _opened_url: Callable[[str], str] | None = None) -> list[str]:
            urls = original(text)
            normalized = [resolve_citation_boundary(url, ledger) for url in urls]
            replacements.update((url, candidate) for url, candidate in zip(urls, normalized, strict=True) if url != candidate)
            return normalized

        with patch.object(provenance, "_urls_in_text", scan):
            after = provenance.citation_problem(summary, ledger=ledger, source_ids=source_ids, require_citation=False)
        return {"before": before, "after": after, "replacements": replacements}

    return [check(summary) for action in base.response_actions(directory / "provider.jsonl")
            if action.get("tool") == "done" and isinstance(args := action.get("args"), dict)
            and isinstance(summary := args.get("summary"), str)]


def resolve_citation_boundary(url: str, ledger: ResearchLedger) -> str:
    """Prefer the full URL, then a unique read URL before Chinese prose."""
    if ledger.canonical_opened_url(url):
        return url
    candidates: set[str] = set()
    for mark in re.finditer(r"[，。；、！？：](?=[\u3400-\u9fff“‘（])", url):
        suffix = url[mark.end():]
        prefix = url[:mark.start()]
        # A swallowed second address must still fail validation as a whole.
        if not re.search(r"https?://", suffix, re.IGNORECASE) and ledger.canonical_opened_url(prefix):
            candidates.add(prefix)
    return next(iter(candidates)) if len(candidates) == 1 else url


class Options(argparse.Namespace):
    run_dir: Path
    base_url: str
    arms: list[str]
    cases: list[str]
    wordings: list[str]
    repeats: int
    max_turns: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    default_arms: list[str] = list(ARMS)
    default_cases: list[str] = list(CASES)
    default_wordings: list[str] = ["original", "paraphrase"]
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=default_arms)
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=default_cases)
    parser.add_argument("--wordings", nargs="+", choices=("original", "paraphrase"), default=default_wordings)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=10)
    args = parser.parse_args(namespace=Options())
    if args.repeats < 1 or args.max_turns < 1 or any(len(values) != len(set(values)) for values in
            (args.arms, args.cases, args.wordings)):
        parser.error("Use positive budgets and distinct arms, cases and wordings")
    source, harness = production_source_digest(), harness_digest()
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target_payload: object = asdict(target)
    write_json(args.run_dir / "manifest.json", {"source_digest": source, "harness_digest": harness,
        "target": decode_json_object(target_payload),
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
