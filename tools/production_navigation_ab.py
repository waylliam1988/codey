"""Existing open_url/open_result A/B through the unmodified production kernel.

Both arms follow detail links using native snapshot refs and real clicks. There
is no candidate-click adapter. Only fixtures, provider wiring and read-only
diagnostic wrappers are injected; completion and receipts remain production.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser_session import BrowserSession, _browser_call
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.tool_requirement import _browser_body
from codey.research.browser_search import BrowserSearchProvider
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)
from tools.navigation_candidate_ab import Arm, Case, document, fixtures, source_fixture
from tools.receipt_fact_delivery_ab import receipt_payload

Kind = Literal["search", "plain_click", "event_click"]
READY = "记录已就绪"
WITNESS = "() => JSON.parse(sessionStorage.getItem('production_clicks') || '[]')"
ELEMENT = """el => ({name:el.getAttribute('aria-label') || el.innerText,
 href:el.href || '', id:el.getAttribute('data-choice') || ''})"""


@dataclass(frozen=True)
class Scenario:
    kind: Kind
    source: Case


@dataclass(frozen=True)
class ExpectedFlow:
    entry: str
    final_url: str
    facts: tuple[str, ...]
    forbidden: tuple[str, ...]
    click_name: str = ""
    click_id: str = ""


def link(name: str, url: str, choice: str, *, body: str = "") -> str:
    record = "sessionStorage.setItem('production_clicks', JSON.stringify(["
    record += "...JSON.parse(sessionStorage.getItem('production_clicks') || '[]'),"
    identity: JsonObject = {"id": choice}
    record += json.dumps(identity)[:-1] + ",trusted:event.isTrusted}]));"
    event = record
    if body:
        event = "event.preventDefault();" + record
        event += "document.querySelector('#record').textContent='加载中';setTimeout(()=>{"
        event += "document.querySelector('#record').textContent=" + json.dumps(body, ensure_ascii=True) + ";},850);"
    return ('<a data-choice="' + html.escape(choice, quote=True) + '" href="' + html.escape(url, quote=True)
            + '" onclick="' + html.escape(event, quote=True) + '">' + html.escape(name) + '</a>')


def production_cases() -> tuple[tuple[Scenario, ExpectedFlow], ...]:
    original = fixtures()
    search = tuple((Scenario("search", case), ExpectedFlow(expected.url, expected.url, expected.facts, expected.forbidden))
                   for case, expected in original[:2])
    detail: list[tuple[Scenario, ExpectedFlow]] = []
    for index, kind in ((2, "plain_click"), (3, "event_click")):
        case, expected = original[index]
        plain = kind == "plain_click"
        name = "订单 A-173 配送详情" if plain else "西仓 RX-08 可用库存详情"
        choice = "a173-delivery" if plain else "west-rx08-stock"
        names = ("订单 A-174 配送详情", "订单 A-173 发票详情") if plain else (
            "北仓 RX-08 可用库存详情", "西仓 RX-09 可用库存详情")
        others = tuple((url, body) for url, body in case.documents if url not in {case.entry, expected.url})
        body = ("记录已就绪。订单 A-173 预计 2026-11-18 送达，追踪号 0042091。" if plain else
                "记录已就绪。西仓 RX-08 可用库存 37 件，记录版本 WEST_0037。")
        listing = '<h1>订单列表</h1>' if plain else '<h1>仓储列表</h1>'
        listing += '<p>查看相应详情，等待加载完成。</p><ul>'
        listing += ''.join('<li>' + link(label, url, "other-" + str(i)) + '</li>'
                           for i, (label, (url, _)) in enumerate(zip(names, others, strict=True)))
        listing += '<li>' + link(name, expected.url, choice, body="" if plain else body) + '</li></ul>'
        listing += '<section id="record">记录尚未展开。</section>'
        destination = document('<h1>' + name + '</h1><p>' + body + '</p>') if plain else document(
            '<h1>静态降级页</h1><p>实时库存未加载，请通过列表页的交互链接查看。</p>')
        docs = ((case.entry, document(listing)), (expected.url, destination), *others)
        hits = (names[0], "订单中心｜订单列表" if plain else "仓储中心｜各仓库存列表", "帮助中心")
        urls = (others[0][0], case.entry, "https://example.com/help/navigation")
        search_html = document('<ol>' + ''.join('<li class="b_algo"><h2><a href="' + html.escape(url, quote=True)
            + '">' + title + '</a></h2><div class="b_caption"><p>'
            + ("在列表中点击对应记录的详情链接查询。" if url == case.entry else "其他记录或一般帮助。")
            + '</p></div></li>' for title, url in zip(hits, urls, strict=True)) + '</ol>')
        fixture = replace(case, documents=docs, search_html=search_html)
        scenario = Scenario("plain_click" if plain else "event_click", fixture)
        detail.append((scenario, ExpectedFlow(case.entry, expected.url if plain else case.entry,
            expected.facts, expected.forbidden, name, choice)))
    return (*search, *detail)


def read_jsonl(path: Path) -> list[JsonObject]:
    return [parse_json_object(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def click_state(browser: BrowserSession, ref: str = "") -> JsonObject:
    def inspect() -> JsonObject:
        value: JsonObject = {}
        try:
            page = browser._page_on_browser_thread()
            raw: object = page.evaluate(WITNESS)
            value["events"] = decode_json_object({"events": raw})["events"]
            if ref:
                _, element = browser._resolve_ref(ref)
                target: object = element.evaluate(ELEMENT)
                value["element"] = decode_json_object(target)
        except Exception as exc:
            value["inspection_error"] = f"{type(exc).__name__}: {exc}"
        return value

    return _browser_call(inspect)


@contextmanager
def observe_execution(directory: Path) -> Generator[None]:
    source_open = ExecutionDelegate._execute_source_open
    browser_action = ExecutionDelegate._execute_browser

    def opened(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools, *, turn: int,
               tool_index: int) -> tuple[ToolResult, bool, int | None]:
        mapping = delegate.session.search_results if delegate.session is not None else {}
        reference = str(call.args.get("result_id", "")).strip().lower()
        url = call.args.get("url") if call.name == "open_url" else mapping.get(reference, "")
        append_json(directory / "source-intents.jsonl", {"call_id": call.call_id, "tool": call.name,
            "args": call.args, "resolved_url": url, "observed_search_results": dict[str, JsonValue](mapping)})
        return source_open(delegate, call, tools, turn=turn, tool_index=tool_index)

    def clicked(delegate: ExecutionDelegate, call: ToolCall, *, turn: int,
                tool_index: int) -> tuple[ToolResult, bool, int | None]:
        browser = delegate.browser_session
        before = click_state(browser, str(call.args.get("ref", ""))) if browser is not None and call.name == "browser_click" else None
        result, ok, code = browser_action(delegate, call, turn=turn, tool_index=tool_index)
        if browser is not None and call.name == "browser_click":
            append_json(directory / "click-observations.jsonl", {"call_id": call.call_id, "before": before,
                "after": click_state(browser), "ok": ok})
        return result, ok, code

    with (patch.object(ExecutionDelegate, "_execute_source_open", opened),
          patch.object(ExecutionDelegate, "_execute_browser", clicked)):
        yield


def source_data(result: ToolResult) -> JsonObject:
    observation = result.canonical.get("research_observation")
    if not isinstance(observation, dict) or not isinstance(observation.get("source"), dict):
        return {}
    return decode_json_object(observation["source"])


def complete_read(result: ToolResult, expected: ExpectedFlow) -> bool:
    if not result.ok or result.truncated:
        return False
    source = source_data(result)
    if source.get("truncated") is not False or source.get("final_url") != expected.final_url:
        return False
    body = _browser_body(result)
    return body is not None and all(fact in body for fact in expected.facts)


def audit_sources(rows: list[EffectReceipt], intents: list[JsonObject], expected: ExpectedFlow, arm: Arm) -> JsonObject:
    tool = "open_url" if arm == "A" else "open_result"
    searches = [row for row in rows if row.result.ok and row.result.call.name == "web_search"]
    verified: list[JsonValue] = []
    for row in rows:
        result = row.result
        if not result.ok or result.call.name != tool:
            continue
        intent = next((item for item in intents if item["call_id"] == result.call.call_id), None)
        source = source_data(result)
        if intent is None:
            continue
        mapping = decode_json_object(intent["observed_search_results"])
        url = intent["resolved_url"]
        if (url == expected.entry and url in mapping.values() and source.get("requested_url") == url
                and source.get("final_url") == url and source.get("truncated") is False):
            verified.append(row.identity)
    return {"expected_interface": tool, "search_count": len(searches), "verified_open_refs": verified,
            "entry_from_observed_candidate": bool(searches and verified), "source_intents": list[JsonValue](intents)}


def audit_clicks(rows: list[EffectReceipt], observations: list[JsonObject], expected: ExpectedFlow) -> JsonObject:
    chains: list[JsonValue] = []
    witnessed: list[JsonValue] = []
    for index, row in enumerate(rows):
        result = row.result
        if result.call.name != "browser_click" or not result.ok:
            continue
        ref = result.call.args.get("ref")
        snapshot = next((prior for prior in reversed(rows[:index]) if prior.result.ok
                         and prior.result.call.name == "browser_snapshot"), None)
        if snapshot is None:
            continue
        value = decode_json_object(snapshot.result.canonical.get("value", {}))
        elements = value.get("elements")
        if not isinstance(elements, list) or not any(isinstance(el, dict) and el.get("ref") == ref
                and el.get("name") == expected.click_name for el in elements):
            continue
        observed: JsonObject = next((item for item in observations if item["call_id"] == result.call.call_id), {})
        after = decode_json_object(observed.get("after", {}))
        events = after.get("events")
        trusted = isinstance(events, list) and any(isinstance(event, dict) and event.get("id") == expected.click_id
                                                  and event.get("trusted") is True for event in events)
        if not trusted:
            continue
        witnessed.append(row.identity)
        wait: EffectReceipt | None = None
        for following in rows[index + 1:]:
            item = following.result
            if item.call.name in {"browser_click", "browser_navigate", "browser_tab_switch", "open_url", "open_result"}:
                break
            data = decode_json_object(item.canonical).get("value")
            if (item.ok and item.call.name == "browser_wait" and isinstance(data, dict) and data.get("matched") is True
                    and data.get("page_url") == expected.final_url and item.call.args.get("condition") == "text"
                    and item.call.args.get("value") == READY):
                wait = following
            if wait is not None and item.call.name == "browser_read" and complete_read(item, expected):
                chains.append([snapshot.identity, row.identity, wait.identity, following.identity])
                break
    return {"trusted_target_click_refs": witnessed, "snapshot_click_wait_read_chains": chains,
            "click_chain_complete": bool(chains)}


def evaluate(directory: Path, rows: list[EffectReceipt], expected: ExpectedFlow, arm: Arm, kind: Kind,
             answer: str, stop_reason: str) -> JsonObject:
    sources = audit_sources(rows, read_jsonl(directory / "source-intents.jsonl"), expected, arm)
    clicks = audit_clicks(rows, read_jsonl(directory / "click-observations.jsonl"), expected)
    read_refs: list[JsonValue] = [row.identity for row in rows if row.result.call.name in {"open_url", "open_result", "browser_read"}
                                and complete_read(row.result, expected)]
    delivered = all(fact in answer for fact in expected.facts) and not any(fact in answer for fact in expected.forbidden)
    passed = (stop_reason == "done" and sources["entry_from_observed_candidate"] is True and bool(read_refs)
              and delivered and (kind == "search" or clicks["click_chain_complete"] is True))
    return {"passed": passed, "sources": sources, "clicks": clicks, "complete_fact_read_refs": read_refs,
            "facts_delivered": delivered}


def run_arm(directory: Path, scenario: Scenario, expected: ExpectedFlow, arm: Arm, target: Target, *, max_turns: int = 10) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    browser = BrowserSearchProvider(engine="bing", headless=True)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    events: list[JsonObject] = []

    def observe(event: dict[str, object]) -> None:
        value = decode_json_object(event)
        events.append(value)
        append_json(directory / "events.jsonl", value)

    def connect(_provider_id: str, **_kwargs: object) -> ExperimentProvider:
        return provider

    source_tool = "open_url" if arm == "A" else "open_result"
    instruction = ("先搜索，从实际结果中选择符合问题的页面。选定后使用 open_url，url 原样填写该结果地址。" if arm == "A" else
                   "先搜索，从实际结果中选择符合问题的页面。选定后使用 open_result，result_id 填搜索回执显示的 rN 引用。")
    required: tuple[str, ...] = ("web_search", source_tool)
    if scenario.kind != "search":
        instruction += ("搜索并打开对应的列表页，再使用 browser_snapshot 观察、browser_click 点击对应详情链接，"
            "用 browser_wait 等待文本“记录已就绪”，最后 browser_read 读取当前页。点击可能触发页面事件，按真实元素引用点击。")
        required += ("browser_snapshot", "browser_click", "browser_wait", "browser_read")
    task = scenario.source.question + "\n" + instruction + "根据成功读取的正文报告用户所问各项信息，用 done 完成交付。不要修改文件或执行命令。"
    selection: dict[str, str] = {"model": target.model, "base_url": target.base_url}
    request = HeadlessRequest(None, task, intent="chat", provider_id="local", max_turns=max_turns,
        state_home=directory / "state", research_store_root=directory / "knowledge",
        requested_capabilities=("web.read", "browser.observe", "browser.interact"), model_selection=selection,
        required_tools=required)
    write_json(directory / "input.json", {"task": task, "question": scenario.source.question,
        "kind": scenario.kind, "arm": arm, "model": target.model, "seed": target.seed,
        "max_turns": max_turns, "required_tools": list(required), "production_digest": production_source_digest()})
    stop_reason = "exception"
    error = ""
    try:
        with (patch.dict(os.environ, dict[str, str]([(NATIVE_TOOLS_ENV, "1")])), source_fixture(browser, scenario.source, directory),
              observe_execution(directory),
              patch("codey.research.search_factory.default_research_search_provider", return_value=browser),
              patch("codey.operations.task_phases.dispatch.default_research_search_provider", return_value=browser),
              patch("codey.app.provider_services.provider_availability", return_value=dict[str, bool](local=True))):
            result = run_headless(request, emit_jsonl=observe, connect_provider=connect)
            stop_reason = result.stop_reason
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        browser.close()
        provider.close()
    terminal = next((event for event in reversed(events) if event.get("type") == "task_done"), {})
    answer = str(terminal.get("summary", ""))
    rows: list[EffectReceipt] = tool_receipts(directory / "state") if (directory / "state/durable.sqlite3").exists() else []
    write_json(directory / "receipts.json", [receipt_payload(row) for row in rows])
    summary: JsonObject = {"case": scenario.source.name, "kind": scenario.kind, "arm": arm, "seed": target.seed,
        "stop_reason": stop_reason, "answer": answer, "error": error,
        **evaluate(directory, rows, expected, arm, scenario.kind, answer, stop_reason),
        **provider_metrics(directory / "provider.jsonl")}
    write_json(directory / "summary.json", summary)
    return summary


class Options(argparse.Namespace):
    run_dir: Path
    pairs: int
    max_turns: int
    base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    if args.pairs < 1 or args.max_turns < 1:
        parser.error("pairs and max-turns must be positive")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target(120, args.base_url)
    before = production_source_digest()
    rows: list[JsonValue] = []
    for pair in range(args.pairs):
        for index, (scenario, expected) in enumerate(production_cases()):
            order: tuple[Arm, Arm] = ("A", "B") if (pair + index) % 2 == 0 else ("B", "A")
            for arm in order:
                print(f"Starting {pair + 1}/{scenario.source.name}/{arm}", flush=True)
                row = run_arm(root / f"pair-{pair + 1}" / scenario.source.name / arm, scenario, expected, arm,
                    replace(target, seed=target.seed + pair), max_turns=args.max_turns)
                rows.append(row)
                progress: JsonObject = {key: row[key] for key in ("case", "arm", "passed", "stop_reason", "model_calls")}
                print(json.dumps(progress), flush=True)
                write_json(root / "progress.json", rows)
    after = production_source_digest()
    write_json(root / "summary.json", {"model": target.model, "production_before": before, "production_after": after,
        "production_unchanged": before == after, "results": rows})
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
