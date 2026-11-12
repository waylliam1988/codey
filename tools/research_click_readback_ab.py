"""Compare receipt-backed post-click reading and scoped records in natural Research."""
from __future__ import annotations

import argparse
import re
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Error, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from codey.automation.browser_session import BrowserSession, _browser_call
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.tool_requirement import _browser_body
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.text_result_views import TextResultSpan
from codey.research.answer_contract import AnswerScope, citation_occurrences
from codey.research.browser_search import BrowserSearchProvider
from codey.research.passages import read_passage_refs
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import research_natural_handoff_ab as natural
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Arm, ExperimentProvider, Target, append_json, write_json
from tools.protocol_surface import script_body

CASES = {
    "visible": natural.CASES["visible"],
    "interactive": replace(natural.CASES["interactive"], html=(
        '<section aria-label="批次 B-19"><h1>批次 B-19</h1><p id="details" aria-busy="false">实时记录尚未加载</p>'
        '<button aria-controls="details" aria-expanded="false" onclick="const p=document.getElementById(\'details\');'
        "this.setAttribute('aria-expanded','true');p.setAttribute('aria-busy','true');"
        "setTimeout(()=>{p.textContent='B-19 状态：已入库。库位：C-42。';p.setAttribute('aria-busy','false');},850)\">"
        '查实时记录</button></section>')),
    "missing": replace(natural.CASES["missing"], html=(
        '<article aria-label="订单 A-173"><h1>订单 A-173</h1><p>当前状态：运输中</p><p id="delivery">预计送达时间未展开</p>'
        '<button aria-controls="delivery" aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\',\'true\');'
        "document.getElementById('delivery').textContent='A-173 预计送达：周五 18:20。'\">查看配送详情</button></article>"
        '<article aria-label="订单 A-174"><h2>订单 A-174</h2><p>预计周四 12:10 送达</p></article>')),
}

_RECORD = """(record) => {
 const nodes = [...document.querySelectorAll('article[aria-label], section[aria-label], tr, [role=row]')];
 const found = nodes.filter(el => (el.getAttribute('aria-label') || el.cells?.[0]?.innerText || '')
     .split(/[^A-Za-z0-9_-]+/).includes(record) && el.getClientRects().length);
 return {count: found.length, text: found.length === 1 ? found[0].innerText : '',
     expandable: found.length === 1 && !!found[0].querySelector('button[aria-controls][aria-expanded=false]')};
}"""
_CONTROL = """button => {
 const id = button.getAttribute('aria-controls');
 const region = id && document.getElementById(id);
 const owner = region?.closest('article[aria-label],section[aria-label],tr,[role=row]');
 if (!region || !owner?.contains(button) || button.getAttribute('aria-expanded') !== 'false') return null;
 return {id, text:region.innerText, owner:owner.getAttribute('aria-label') || owner.cells?.[0]?.innerText || ''};
}"""
_READY = """({id,text}) => {
 const region = document.getElementById(id);
 return !!region && region.getAttribute('aria-busy') !== 'true' && region.getClientRects().length > 0
     && !!region.innerText.trim() && region.innerText !== text;
}"""


@dataclass(frozen=True)
class Click:
    call_id: str
    document: JsonObject
    region: JsonObject


def record_id(question: str) -> str:
    ids = set(re.findall(r"(?<![\w-])[A-Z]+-\d+(?![\w-])", question))
    return next(iter(ids)) if len(ids) == 1 else ""


def projection(page: Page, record: str) -> JsonObject:
    return decode_json_object(page.evaluate(_RECORD, record))


def unique_text(selected: JsonObject) -> str | None:
    text = selected.get("text")
    return text if selected.get("count") == 1 and isinstance(text, str) and 0 < len(text.strip()) <= 6000 else None


def click_candidate(browser: BrowserSession, call: ToolCall, record: str) -> Click | None:
    def inspect() -> Click | None:
        try:
            page, handle = browser._resolve_ref(str(call.args.get("ref") or ""))
            raw: object = handle.evaluate(_CONTROL)
            if not isinstance(raw, dict) or unique_text(projection(page, record)) is None:
                return None
            region = decode_json_object(raw)
            owner = str(region.get("owner") or "")
            document = browser._document_identity_on_browser_thread()
            if document is not None and record in re.split(r"[^A-Za-z0-9_-]+", owner):
                return Click(call.call_id, document, region)
        except (Error, RuntimeError, ValueError):
            return None
        return None

    return _browser_call(inspect)


def wait_for_readback(browser: BrowserSession, click: Click) -> str | None:
    def wait() -> str | None:
        if browser._document_identity_on_browser_thread() != click.document:
            return None
        page = browser._page_on_browser_thread()
        try:
            page.wait_for_function(_READY, arg=click.region, timeout=1600)
        except (PlaywrightTimeout, Error):
            return None
        if browser._document_identity_on_browser_thread() != click.document:
            return None
        text: object = page.evaluate("id => document.getElementById(id)?.innerText", click.region["id"])
        return text if isinstance(text, str) and text.strip() else None

    return _browser_call(wait)


def cites_post_click(browser: BrowserSession, click: Click | None, rows: tuple[ToolResult, ...], document: JsonObject,
                    cited: set[str]) -> bool:
    if click is None or click.document != document:
        return True

    def changed_region() -> object:
        page = browser._page_on_browser_thread()
        return page.evaluate(_READY, click.region) and page.evaluate(
            "id => document.getElementById(id)?.innerText", click.region["id"])

    updated = _browser_call(changed_region)
    index = next((i for i, result in enumerate(rows) if result.ok and result.call.call_id == click.call_id), None)
    if not isinstance(updated, str) or not updated.strip() or index is None:
        return False
    return any(result.ok and result.call.name == "browser_read" and result.audit.get("browser_document") == document
               and natural.contains_fact(_browser_body(result) or "", updated)
               and bool(cited.intersection(read_passage_refs(result.canonical.get("read_passages", []))))
               for result in rows[index + 1:])


@contextmanager
def readback_experiment(directory: Path, arm: Arm, case: natural.Case, *,
                        automatic_readback: bool = True, guidance: bool = True) -> Generator[None, None, None]:
    execute, opened, submit = (ExecutionDelegate._execute_browser, ExecutionDelegate._execute_source_open,
                               ExecutionDelegate._submit_research_answer)
    fetch, generate, freeze = BrowserSearchProvider._fetch_on_browser_thread, ExperimentProvider._generate, KernelContext.freeze
    record = record_id(case.question)
    active: BrowserSession | None = None
    pending: Click | None = None
    last_click: Click | None = None
    automatic: str | None = None
    source_scoped = False
    hinted = False
    read_available = False

    def fetch_record(provider: BrowserSearchProvider, url: str) -> dict[str, object]:
        nonlocal source_scoped
        result = fetch(provider, url)
        page = provider._fetch_page
        if result.get("status") == "ok" and page is not None and page.url == case.url:
            text = unique_text(projection(page, record))
            if text is not None and result.get("truncated") is False:
                source_scoped = True
                return {**result, "text": text}
        return result

    def source(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools, *, turn: int,
               tool_index: int) -> tuple[ToolResult, bool, int | None]:
        nonlocal active, source_scoped
        active = delegate.browser_session
        source_scoped = False
        result, ok, code = opened(delegate, call, tools, turn=turn, tool_index=tool_index)
        if ok and source_scoped and active is not None:
            result = replace(result, audit={**result.audit, "record_id": record, "browser_document": active.document_identity()})
        return result, ok, code

    def browser_call(delegate: ExecutionDelegate, call: ToolCall, *, turn: int,
                     tool_index: int) -> tuple[ToolResult, bool, int | None]:
        nonlocal active, pending, last_click, automatic
        active = browser = delegate.browser_session
        if browser is None:
            return execute(delegate, call, turn=turn, tool_index=tool_index)
        candidate = click_candidate(browser, call, record) if call.name == "browser_click" else None
        if call.name != "browser_read":
            result, ok, code = execute(delegate, call, turn=turn, tool_index=tool_index)
            if ok and call.name in natural.CHANGES:
                pending = last_click = None
            if candidate is not None and ok and browser.document_identity() == candidate.document:
                pending = last_click = candidate
            return result, ok, code
        document = browser.document_identity()
        scoped = False
        capture = browser.capture

        def capture_record() -> dict[str, object]:
            nonlocal scoped
            result = capture()
            if document is not None and document == browser.document_identity() and document.get("url") == case.url:
                text = _browser_call(lambda: unique_text(projection(browser._page_on_browser_thread(), record)))
                if text is not None and result.get("status") == "ok" and result.get("truncated") is False:
                    scoped = True
                    return {**result, "text": text}
            return result

        with patch.object(browser, "capture", capture_record):
            result, ok, code = execute(delegate, call, turn=turn, tool_index=tool_index)
        if ok and scoped:
            audit = {**result.audit, "record_id": record, "browser_document": document}
            if automatic is not None:
                audit["readback_of"] = automatic
            result = replace(result, audit=audit)
        if automatic is not None:
            append_json(directory / "readback.jsonl", {"event": "read", "click": automatic, "ok": ok and scoped})
            automatic = None
        return result, ok, code

    def send(provider: ExperimentProvider, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
             *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
             tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
             tool_call_contract: str = "", tool_call_observations: str = "", tool_call_history_groups: int = 0,
             output_token_limit: int = 0, require_tool_call: bool = False) -> dict[str, object]:
        nonlocal pending, automatic
        if automatic_readback and pending is not None and active is not None:
            click, pending = pending, None
            receipts = tool_receipts(directory / "state")
            index = next((i for i, row in enumerate(receipts) if row.result.ok and row.result.call.call_id == click.call_id), None)
            if index is not None and read_available:
                ref = receipts[index].identity
                updated = wait_for_readback(active, click)
                already_read = updated is not None and any(
                    row.result.ok and row.result.call.name == "browser_read"
                    and row.result.audit.get("browser_document") == click.document
                    and natural.contains_fact(_browser_body(row.result) or "", updated) for row in receipts[index + 1:])
                append_json(directory / "readback.jsonl", {"event": "readiness", "click": ref, "ready": updated is not None,
                    "already_read": already_read})
                if updated is not None and not already_read:
                    automatic = ref
                    provider.exchange += 1
                    action: JsonObject = {"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_read({}));"}}
                    append_json(directory / "provider.jsonl", {"type": "runtime_action", "exchange": provider.exchange, "action": action})
                    return script_body(action)
                if updated is None:
                    messages = [*messages, {"role": "user", "content": "The clicked region has no confirmed updated text yet. "
                        "Keep the requested detail unresolved and continue gathering current evidence."}]
        return generate(provider, messages, tools, timeout=timeout, checkpoints=checkpoints, tool_call_json=tool_call_json,
            tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract, tool_call_observations=tool_call_observations,
            tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
            require_tool_call=require_tool_call)

    def submission(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        scope, browser, session = delegate._research_scope, delegate.browser_session, delegate.session
        if isinstance(scope, AnswerScope) and browser is not None and session is not None and call.args.get("state") == "answered":
            document = browser.document_identity()
            if document is not None and document.get("url") == case.url:
                selected = _browser_call(lambda: projection(browser._page_on_browser_thread(), record))
                count = selected.get("count")
                labels = {row["label"] for row in citation_occurrences(str(call.args.get("markdown") or ""))}
                rows = tuple(session._memory_results.values())
                passages = {ref for row in rows if row.ok and row.audit.get("record_id") == record
                            and row.audit.get("browser_document") == document
                            for ref in read_passage_refs(row.canonical.get("read_passages", []))}
                cited = {ref for label, ref in scope.aliases if label in labels}
                if count and (count != 1 or not cited or not cited <= passages
                              or not cites_post_click(browser, last_click, rows, document, cited)):
                    append_json(directory / "readback.jsonl", {"event": "unbound_submission", "record": record, "count": count})
                    return ToolResult(call=call, ok=False, model_text="The target record needs its own unambiguous, cited read. "
                                      "Continue gathering evidence or report the uncertainty."), False, None
        return submit(delegate, call, tools)

    def prepare(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int,
                prompt: str = "", messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
                counters: RuntimeExecutionCounters | None = None, text_result_spans: tuple[TextResultSpan, ...] = (),
                coding_context_prepared: bool = False, initial_source_batch: bool = False) -> None:
        nonlocal hinted, read_available
        read_available = "codemode" in snapshot.tool_names and "browser_read" in snapshot.nested_tool_names
        browser = active
        if guidance and not hinted and browser is not None and isinstance(snapshot.research_scope, AnswerScope):
            document = browser.document_identity()
            if document is not None and document.get("url") == case.url:
                selected = _browser_call(lambda: projection(browser._page_on_browser_thread(), record))
                if selected.get("expandable") is True:
                    prompt += ("\n\nThe requested record has an observed expandable detail control. If a requested fact is "
                               "missing, inspect and use the relevant control before answering. The runtime can read back its updated region.")
                    hinted = True
        freeze(context, state, snapshot, turn=turn, prompt=prompt, messages=messages, acknowledge=acknowledge,
            counters=counters, text_result_spans=text_result_spans, coding_context_prepared=coding_context_prepared,
            initial_source_batch=initial_source_batch)

    if arm == "A" or not record:
        yield
    else:
        with (patch.object(BrowserSearchProvider, "_fetch_on_browser_thread", fetch_record),
              patch.object(ExecutionDelegate, "_execute_source_open", source),
              patch.object(ExecutionDelegate, "_execute_browser", browser_call),
              patch.object(ExecutionDelegate, "_submit_research_answer", submission),
              patch.object(ExperimentProvider, "_generate", send), patch.object(KernelContext, "freeze", prepare)):
            yield


def run_arm(directory: Path, case: natural.Case, arm: Arm, target: Target, *, max_turns: int = 10,
            seeded_click: str = "") -> JsonObject:
    generate = ExperimentProvider._generate
    used_seed = False

    def seed(provider: ExperimentProvider, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
             *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
             tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
             tool_call_contract: str = "", tool_call_observations: str = "", tool_call_history_groups: int = 0,
             output_token_limit: int = 0, require_tool_call: bool = False) -> dict[str, object]:
        nonlocal used_seed
        if not used_seed:
            used_seed = True
            provider.exchange += 1
            action: JsonObject = {"tool": "codemode", "args": {"code": "const s=unwrap(await tools.browser_snapshot({}));"
                "return unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name===inputs.label).ref}));",
                "inputs": {"label": seeded_click}}}
            append_json(directory / "provider.jsonl", {"type": "scripted_click", "exchange": provider.exchange, "action": action})
            return script_body(action)
        return generate(provider, messages, tools, timeout=timeout, checkpoints=checkpoints, tool_call_json=tool_call_json,
            tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract, tool_call_observations=tool_call_observations,
            tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
            require_tool_call=require_tool_call)

    def configure(path: Path, group: Arm) -> AbstractContextManager[None]:
        return readback_experiment(path, group, case)

    with (patch.object(ExperimentProvider, "_generate", seed) if seeded_click else nullcontext(),
          patch.object(natural, "experiment", configure)):
        row = natural.run_arm(directory, case, arm, target, max_turns=max_turns)
    receipts = [receipt.result for receipt in tool_receipts(directory / "state")]
    automatic = [result for result in receipts if result.ok and result.call.name == "browser_read"
                 and result.audit.get("readback_of")]
    actual = [result for result in receipts if result.ok and result.call.name in natural.READS and result.audit.get("record_id")]
    scoped_texts: list[JsonValue] = [_browser_body(result) or "" for result in actual]
    row.update(seeded_click=bool(seeded_click), automatic_reads=len(automatic), scoped_reads=len(actual),
        scoped_texts=scoped_texts,
        accepted_clicks=sum(result.ok and result.call.name == "browser_click" for result in receipts))
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--seed-click", action="store_true", help="Run the two interactive scenarios with an identical scripted initial click.")
    args = parser.parse_args()
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    runs: list[JsonObject] = []
    matrix: tuple[tuple[Arm, Arm], ...] = (("A", "B"), ("B", "A"), ("A", "B"))
    for (name, case), order in zip(CASES.items(), matrix, strict=True):
        if args.seed_click and name == "visible":
            continue
        label = {"interactive": "查实时记录", "missing": "查看配送详情"}.get(name, "") if args.seed_click else ""
        for arm in order:
            print(f"Starting {name}/{arm}", flush=True)
            row = run_arm(args.run_dir / f"{name}-{arm}", case, arm, target, seeded_click=label)
            runs.append({"case": name, **row})
            write_json(args.run_dir / "summary.json", {"model": target.model, "runs": runs})
            print({key: row[key] for key in ("passed", "stop_reason", "model_calls", "automatic_reads", "scoped_reads")}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
