"""Compare a direct control choice, automatic readback and examples in strict Research."""
from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from playwright.sync_api import Error

from codey.automation.browser_session import BrowserSession, _browser_call
from codey.operations import kernel_protocol
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot
from codey.protocols.text_turn import DecodedTextTurn, TextError, TextRequest, TextRequests
from codey.providers.base import AssistantTurn, ProviderToolResult
from codey.providers.text_result_views import TextResultSpan
from codey.runtime.core.models import ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, json_contract_text, validate_args_with_spec
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import research_click_readback_ab as readback
from tools import research_natural_handoff_ab as natural
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Arm as NaturalArm
from tools.completion_explanation_ab import Target, append_json, write_json

TOOL = "inspect_page_detail"
PROGRAM = "return unwrap(await tools.browser_click({ref:inputs.ref}));"
CASES = {
    **readback.CASES,
    "visible": replace(readback.CASES["visible"], html=(
        '<section aria-label="馆藏 LY-72">' + readback.CASES["visible"].html
        + '<p id="info">馆藏说明未展开</p><button aria-controls="info" aria-expanded="false" '
        "onclick=\"this.setAttribute('aria-expanded','true');document.getElementById('info').textContent='馆藏位于一层。'\">"
        '查看馆藏详情</button></section>')),
    "missing": replace(readback.CASES["missing"], html=readback.CASES["missing"].html.replace(
        '<p>预计周四 12:10 送达</p>', '<p id="other">预计周四 12:10 送达</p>'
        '<button aria-controls="other" aria-expanded="false" '
        "onclick=\"this.setAttribute('aria-expanded','true');document.getElementById('other').textContent='A-174 周四 12:10。'\">"
        '查看配送详情</button>')),
}
PARAPHRASES = {
    "interactive": "帮我看看 B-19 这批货入库了吗，现在放在哪个库位？",
    "missing": "帮我看看 A-173 现在到哪一步了，大概几点能到？",
}
GUIDANCE = ("If a requested fact is absent and a relevant detail control is available, inspect the control before answering. "
            "If the current evidence already answers the question, submit the answer.")
EXAMPLES = (
    "Examples for deciding the next action (illustrative only).\n"
    "User asks for a train's platform. The current record already gives the platform. Submit with its evidence.\n"
    "User asks for an item's current pickup time. Its record says details are collapsed and offers a detail control. "
    "Choose the control in that item's record and use the updated evidence. If the attempt still leaves the time unavailable, "
    "report that limitation. A time shown under another item cannot answer the question."
)


@dataclass(frozen=True)
class Arm:
    choices: bool = False
    automatic: bool = False
    examples: bool = False


ARMS = {"A": Arm(), "B": Arm(choices=True), "C": Arm(choices=True, automatic=True),
        "D": Arm(choices=True, automatic=True, examples=True)}


def owner_tokens(owner: str) -> set[str]:
    return set(re.split(r"[^A-Za-z0-9_-]+", owner))


def detail_region(browser: BrowserSession, ref: str) -> JsonObject | None:
    try:
        _, handle = browser._resolve_ref(ref)
        if not handle.is_visible() or not handle.is_enabled():
            return None
        raw: object = handle.evaluate(readback._CONTROL)
        return decode_json_object(raw) if raw is not None else None
    except (Error, RuntimeError, ValueError):
        return None


def observed_controls(browser: BrowserSession, document: JsonObject) -> list[JsonObject]:
    def capture() -> list[JsonObject]:
        result = browser.snapshot(max_elements=24)
        elements = result.observation.get("elements")
        if not result.ok or result.observation.get("elements_truncated") is not False or not isinstance(elements, list):
            return []
        rows: list[JsonObject] = []
        for raw in elements:
            element = decode_json_object(raw)
            ref = element.get("ref")
            if (element.get("role") == "button" and element.get("disabled") is False and isinstance(ref, str)
                    and (region := detail_region(browser, ref)) is not None
                    and isinstance(owner := region.get("owner"), str) and owner):
                rows.append({"choice": f"detail-{len(rows) + 1}", "label": element["name"], "record": owner,
                             "region": region["id"], "ref": ref})
        return rows if browser.document_identity() == document else []

    return _browser_call(capture)


def choice_snapshot(snapshot: TurnSnapshot, document: JsonObject, rows: list[JsonObject]) -> TurnSnapshot:
    spec = freeze_spec_parameters(ToolSpec(
        name=TOOL, description="Choose an observed detail button by its choice ID. The runtime handles its browser reference.",
        parameters=(("choice", {"type": "string", "enum": [row["choice"] for row in rows]}),), required=("choice",),
        declarations=json.dumps({"document": document, "controls": rows}, ensure_ascii=False),
    ))
    position = len(snapshot.tool_names)
    return replace(snapshot, tool_names=(*snapshot.tool_names, TOOL),
        frozen_specs=(*snapshot.frozen_specs[:position], spec, *snapshot.frozen_specs[position:]),
        custom_executors=(*snapshot.custom_executors[:position], (TOOL, None), *snapshot.custom_executors[position:]),
        contract_text=json_contract_text((*snapshot.model_specs, public_spec(spec))))


def public_spec(spec: ToolSpec) -> ToolSpec:
    view = decode_json_object(json.loads(spec.declarations))
    controls = view["controls"]
    assert isinstance(controls, list)
    rows = [decode_json_object(raw) for raw in controls]
    return replace(spec, declarations=json.dumps(
        [{key: row[key] for key in ("choice", "label", "record")} for row in rows], ensure_ascii=False))


def control_prompt(rows: list[JsonObject]) -> str:
    lines = ["当前页面可直接选择的详情操作。需要补查时，选用户所问记录里的按钮。"]
    for row in rows:
        action = {"tool": TOOL, "args": {"choice": row["choice"]}}
        lines.append(f"- {row['record']} 内的按钮「{row['label']}」　{json.dumps(action, ensure_ascii=False)}")
    return "\n".join(lines)


@contextmanager
def experiment(directory: Path, case: natural.Case, arm: Arm) -> Generator[None, None, None]:
    record = readback.record_id(case.question)
    active: BrowserSession | None = None
    with readback.readback_experiment(directory, "B", case, automatic_readback=arm.automatic, guidance=False):
        freeze, normalize = KernelContext.freeze, kernel_protocol.normalize_turn
        descriptor: object = vars(TurnSnapshot)["model_specs"]
        assert isinstance(descriptor, property) and descriptor.fget is not None
        original_specs = cast("Callable[[TurnSnapshot], tuple[ToolSpec, ...]]", descriptor.fget)

        def specs(snapshot: TurnSnapshot) -> tuple[ToolSpec, ...]:
            return tuple(public_spec(spec) if spec.name == TOOL else spec for spec in original_specs(snapshot))

        def prepare(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int,
                    prompt: str = "", messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
                    counters: RuntimeExecutionCounters | None = None, text_result_spans: tuple[TextResultSpan, ...] = (),
                    coding_context_prepared: bool = False, initial_source_batch: bool = False) -> None:
            nonlocal active
            research = context.execution.research_tools
            active = research.browser_session if research is not None else None
            if not context.native and not acknowledge and snapshot.research_scope is not None:
                prompt += "\n\n" + GUIDANCE
                if arm.examples:
                    prompt += "\n\n" + EXAMPLES
                if (arm.choices and active is not None and "codemode" in snapshot.tool_names
                        and "browser_click" in snapshot.nested_tool_names
                        and any(row.ok and row.call.name in natural.READS for row in context.session._memory_results.values())
                        and (document := active.document_identity()) is not None and document.get("url") == case.url
                        and (rows := observed_controls(active, document))):
                    snapshot = choice_snapshot(snapshot, document, rows)
                    prompt += "\n\n" + control_prompt(rows)
                    append_json(directory / "choices.jsonl", {"event": "offered", "turn": turn, "controls": rows})
            freeze(context, state, snapshot, turn=turn, prompt=prompt, messages=messages, acknowledge=acknowledge,
                counters=counters, text_result_spans=text_result_spans, coding_context_prepared=coding_context_prepared,
                initial_source_batch=initial_source_batch)

        def lower(reply: DecodedTextTurn | AssistantTurn, *, snapshot: TurnSnapshot) -> ToolPlan:
            if isinstance(reply, TextRequests) and any(item.tool == TOOL for item in reply.items):
                spec = next((spec for spec in snapshot.frozen_specs if spec.name == TOOL), None)
                if spec is None or len(reply.items) != 1 or active is None:
                    return normalize(TextError("Choose one currently offered detail action.", tool=TOOL), snapshot=snapshot)
                args = reply.items[0].args
                if error := validate_args_with_spec(spec, args):
                    return normalize(TextError(error, tool=TOOL), snapshot=snapshot)
                view = decode_json_object(json.loads(spec.declarations))
                controls = view["controls"]
                assert isinstance(controls, list)
                row = next(decode_json_object(raw) for raw in controls if decode_json_object(raw)["choice"] == args["choice"])
                ref = str(row["ref"])
                browser = active

                def matches_current_record() -> bool:
                    if browser.document_identity() != view["document"]:
                        return False
                    region = detail_region(browser, ref)
                    return (region is not None and region["id"] == row["region"] and region["owner"] == row["record"]
                            and record in owner_tokens(str(row["record"]))
                            and readback.unique_text(readback.projection(browser._page_on_browser_thread(), record)) is not None)

                valid = _browser_call(matches_current_record)
                append_json(directory / "choices.jsonl", {"event": "selected", "choice": args["choice"],
                            "record": row["record"], "valid": valid})
                if not valid:
                    return normalize(TextError("The control is stale or belongs to a different record. "
                                               "Choose the current target record's detail control.", tool=TOOL), snapshot=snapshot)
                reply = TextRequests((TextRequest("codemode", {"code": PROGRAM, "inputs": {"ref": ref}}),))
            return normalize(reply, snapshot=snapshot)

        with (patch.object(TurnSnapshot, "model_specs", property(specs)), patch.object(KernelContext, "freeze", prepare),
              patch.object(kernel_protocol, "normalize_turn", lower)):
            yield


def run_arm(directory: Path, case: natural.Case, arm: str, target: Target, *, max_turns: int = 7) -> JsonObject:
    def configure(path: Path, _arm: NaturalArm) -> AbstractContextManager[None]:
        return experiment(path, case, ARMS[arm])

    with patch.object(natural, "experiment", configure):
        row = natural.run_arm(directory, case, "A", target, max_turns=max_turns)
    receipts = [entry.result for entry in tool_receipts(directory / "state")]
    trace = directory / "choices.jsonl"
    choices = [decode_json_object(json.loads(line)) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
    clicks = [entry for entry in receipts if entry.ok and entry.call.name == "browser_click"]
    selections = selection_events(choices)
    row.update(arm=arm, offered_turns=sum(entry["event"] == "offered" for entry in choices),
               selections=selections, successful_clicks=len(clicks),
               automatic_reads=sum(entry.ok and entry.call.name == "browser_read" and bool(entry.audit.get("readback_of"))
                                   for entry in receipts),
               avoided_other_record=case.path != CASES["missing"].path
               or not natural.contains_fact(str(row["answer"]), "周四 12:10"))
    row["passed"] = passes_case(row, case, receipts)
    write_json(directory / "summary.json", row)
    return row


def answer_contains_fact(answer: str, fact: str) -> bool:
    if fact == "已入库":
        answer = answer.replace("已经入库", "已入库")
    return natural.contains_fact(answer, fact)


def passes_case(row: JsonObject, case: natural.Case, receipts: list[ToolResult]) -> bool:
    cited_text = natural.cited_page_text(receipts, case)
    expected_clicks = 0 if case.path == CASES["visible"].path else 1
    return (row["source_stable"] is True and row["stop_reason"] == "done" and row["avoided_other_record"] is True
            and row["successful_clicks"] == expected_clicks
            and all(answer_contains_fact(str(row["answer"]), fact) and natural.contains_fact(cited_text, fact)
                    for fact in case.facts))


def selection_events(trace: list[JsonObject]) -> list[JsonValue]:
    selections: list[JsonValue] = []
    turn: JsonValue = None
    seen: set[tuple[int, str]] = set()
    for entry in trace:
        if entry["event"] == "offered":
            turn = entry["turn"]
        elif entry["event"] == "selected" and type(turn) is int:
            key = (turn, str(entry["choice"]))
            if key not in seen:
                selections.append({**entry, "turn": turn})
                seen.add(key)
    return selections


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--paraphrases", action="store_true", help="Repeat the two interactive scenarios with alternative wording.")
    args = parser.parse_args()
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    runs: list[JsonObject] = []
    orders: tuple[tuple[str, ...], ...] = (("A", "B", "C", "D"), ("B", "C", "D", "A"), ("C", "D", "A", "B"))
    for (name, case), order in zip(CASES.items(), orders, strict=True):
        if args.paraphrases:
            if name not in PARAPHRASES:
                continue
            case = replace(case, question=PARAPHRASES[name])
            order = tuple(arm for arm in order if arm != "B")
        for arm in order:
            print(f"Starting {name}/{arm}", flush=True)
            row = run_arm(args.run_dir / f"{name}-{arm}", case, arm, target)
            runs.append({"case": name, **row})
            write_json(args.run_dir / "summary.json", {"model": target.model, "seed": target.seed, "runs": runs})
            print({key: row[key] for key in ("passed", "stop_reason", "model_calls", "successful_clicks",
                                            "automatic_reads", "selections")}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
