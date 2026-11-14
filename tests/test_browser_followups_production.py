"""Production browser follow-ups through the normal provider and receipt chain."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from playwright.sync_api import Route, sync_playwright

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.browser_followups import DIRECT_TOOLS, TEXT_SOURCE, explicit_fill_text
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn, offer_browser_tools
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_structured_actions, require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.research.provenance import _urls_in_text
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import RunEvent
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.support.browser_reconnect_process import URL, FixtureBrowser, NoModel, new_session, resources
from tests.support.real_cdp import cdp_browser
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_leaf_exposure_ab import CASES, NOTE, Case, run_arm
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Target

QUESTION = f'请保留引号和换行录入这段备注，查它有没有受理，回执号是多少？请注明来源。\n{NOTE}'
TARGET = Target("http://scripted.test/v1", "fixture", 123456789, 15)
GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


def test_snapshot_freezes_text_without_changing_authority() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat", task_text=QUESTION)
    base = build_turn_snapshot(session)
    offered = offer_browser_tools(base, session)
    restored = restore_snapshot(snapshot_description(offered))
    assert restored == offered and restored.browser_fill_text == NOTE
    assert base.nested_tool_names == restored.nested_tool_names
    assert set(restored.tool_names) - set(base.tool_names) == set(DIRECT_TOOLS)
    assert {s.name: s for s in base.frozen_specs} == {s.name: s for s in restored.frozen_specs}
    for args in ('{"ref":"observed","source":"wrong"}', '{"ref":"observed","value":"partial"}'):
        reply = decode_text_turn('{"tool":"browser_fill","args":' + args + '}')
        assert normalize_turn(reply, snapshot=restored).protocol_error
    session.controller_denied = ("browser_read",)
    restricted = build_turn_snapshot(session)
    assert offer_browser_tools(restricted, session) == restricted
    assert explicit_fill_text(QUESTION + "\n\nAnother paragraph.") is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("wording", ["two_lines", "code_block", "following_block"])
def test_bound_fill_and_query_commit_once_with_chinese_url_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wording: str,
) -> None:
    path = tmp_path / "run"

    def ref(name: str) -> str:
        rows = [row.result for row in tool_receipts(path / "state") if row.result.call.name == "browser_snapshot" and row.result.ok]
        elements = decode_json_object(rows[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row.get("name") == name)

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "browser_snapshot", "args": {}}
        if wording == "code_block":
            yield {"tool": "codemode", "args": {"action": {"kind": "fill",
                "target": {"ref": ref("备注")}, "source": TEXT_SOURCE}}}
        else:
            yield {"tool": "browser_fill", "args": {"ref": ref("备注"), "source": TEXT_SOURCE}}
        yield {"tool": "browser_click", "args": {"ref": ref("查询记录"), "purpose": "result"}}
        yield {"tool": "done", "args": {
            "summary": "已打开 https://example.com/leaf-exposure-record，并读取结果。备注已受理，回执码 NOTE_624。[s1]"}}

    install_http(monkeypatch, replies())
    question = {
        "two_lines": f"备注请原样输入下面两行，再查询受理状态和回执码，附来源。\n{NOTE}",
        "code_block": f"请把代码块里的原文填入备注框，再查询受理状态和回执码，附来源。\n```text\n{NOTE}\n```",
        "following_block": QUESTION,
    }[wording]
    fixture = CASES["quoted"]
    required = (*fixture.required, require_structured_actions()) if wording == "code_block" else fixture.required
    result = run_arm(path, replace(fixture, question=question, required=required), "A", TARGET, max_turns=4)
    assert result["stop_reason"] == "done" and result["model_calls"] == 4, result
    assert "NOTE_624" in str(result["answer"]), result
    assert decode_json_object(result["page_state"]) == {"clicks": 1, "value": NOTE}
    receipts = tool_receipts(path / "state")
    clicks = [row for row in receipts if row.result.call.name == "browser_click"]
    reads = [row.result for row in receipts if row.result.call.name == "browser_read" and row.result.ok]
    assert len(clicks) == len(reads) == 1
    assert reads[0].audit["query_click"] == clicks[0].identity
    assert any(decode_json_object(decode_json_object(item["payload"])["snapshot"]).get("browser_fill_text") == NOTE
               for _, item in artifacts(path / "state") if item.get("kind") == "provider_request")


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_delayed_result_is_evidence_only_after_declared_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "async"
    html = """<article id="record">Previous result: OLD_17.</article><button onclick="
        window.clicks=(window.clicks||0)+1; const record=document.getElementById('record');
        record.setAttribute('aria-busy','true');
        setTimeout(() => {record.textContent='Current result: NEW_29.';
           record.setAttribute('data-ready','yes'); record.removeAttribute('aria-busy');}, 650)">Query</button>"""
    wait = {"condition": "selector", "value": "#record[data-ready=yes]", "timeout_ms": 5000}
    case = Case("查询实时结果并附来源。", html, ("NEW_29",),
                ("browser_click", "browser_read", require_tool_success("browser_wait", arguments=wait)))

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "browser_snapshot", "args": {}}
        snapshot = next(row.result for row in tool_receipts(path / "state") if row.result.call.name == "browser_snapshot")
        elements = decode_json_object(snapshot.canonical["value"])["elements"]
        assert isinstance(elements, list)
        ref = decode_json_object(elements[0])["ref"]
        yield {"tool": "browser_click", "args": {"ref": ref, "purpose": "result"}}
        yield {"tool": "done", "args": {"summary": "查询结果 NEW_29。[s1]"}}

    install_http(monkeypatch, replies())
    result = run_arm(path, case, "A", TARGET, max_turns=3)
    assert result["stop_reason"] == "done" and decode_json_object(result["page_state"])["clicks"] == 1, result
    receipts = [row.result for row in tool_receipts(path / "state")]
    relevant = [row for row in receipts if row.call.name in {"browser_click", "browser_wait", "browser_read"}]
    assert [row.call.name for row in relevant] == ["browser_click", "browser_wait", "browser_read"]
    assert all(row.ok for row in relevant)
    assert "NEW_29" in relevant[-1].model_text and "OLD_17" not in relevant[-1].model_text


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_opening_controls_returns_references_without_a_formal_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "controls"
    case = Case("打开筛选菜单，再查询详情并附来源。", """<button id="menu">Menu</button>
        <button id="detail" hidden>Detail</button><article id="record">No record loaded yet.</article>
        <script>
        const menu = document.getElementById('menu'), detail = document.getElementById('detail');
        menu.onclick = () => {window.clicks = 1; detail.hidden = false;};
        detail.onclick = () => {window.clicks += 1;
          document.getElementById('record').textContent = 'Current record DETAIL_29 is ready.';};
        </script>""", ("DETAIL_29",), ("browser_click", "browser_read"))

    def latest_ref(name: str) -> str:
        snapshots = [item.result for item in tool_receipts(path / "state") if item.result.call.name == "browser_snapshot"]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row.get("name") == name)

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "browser_snapshot", "args": {}}
        yield {"tool": "browser_click", "args": {"ref": latest_ref("Menu"), "purpose": "controls"}}
        yield {"tool": "browser_click", "args": {"ref": latest_ref("Detail"), "purpose": "result"}}
        yield {"tool": "done", "args": {"summary": "当前记录是 DETAIL_29。[s1]"}}

    install_http(monkeypatch, replies())
    result = run_arm(path, case, "A", TARGET, max_turns=4)
    assert result["stop_reason"] == "done" and decode_json_object(result["page_state"])["clicks"] == 2, result
    receipts = [row.result.call.name for row in tool_receipts(path / "state")]
    assert [name for name in receipts if name in DIRECT_TOOLS] == [
        "browser_snapshot", "browser_click", "browser_snapshot", "browser_click", "browser_read"]


def test_citation_boundary_preserves_full_urls_and_rejects_ambiguous_suffixes() -> None:
    base = "https://example.com/记录"
    longer = base + "，正文"
    opened = {base: base, longer: longer}
    def lookup(url: str) -> str:
        return opened.get(url, "")

    assert _urls_in_text(longer, lookup) == [longer]
    assert _urls_in_text(base + "，并阅读", lookup) == [base]
    assert _urls_in_text(longer + "，还有", lookup) == [longer + "，还有"]
    second = base + "，然后https://example.org/other"
    assert _urls_in_text(second, lookup) == [second]


def query_restart_probe(state: Path, port: int, stage: str) -> None:
    initial = stage != "resume"
    sink = resources(state, initialize=initial)
    session = new_session()

    class QueryBrowser(FixtureBrowser):
        def _guard_request(self, route: Route) -> None:
            route.fulfill(status=200, content_type="text/html", body="""<article id="record">Previous record 17 grams.</article>
                <button onclick="this.dataset.count=Number(this.dataset.count||0)+1;
                document.getElementById('record').textContent='Current record is 29 grams.'">Query</button>""")

    provider = QueryBrowser(headless=False, isolated=False, launch=False, require_existing_session=True, cdp_port=port)
    browser = BrowserSession(provider)
    tools = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    context = KernelContext(session, KernelRunRequest(
        transport=KernelTransportDeps(provider=NoModel(), run_id="r", effect_scope="task", provider_id="local"),
        execution=KernelExecutionDeps(intent_sink=sink, research_tools=tools)))
    context.restore_facts()

    def stop_after_click(event: RunEvent) -> None:
        if event.kind == "tool" and event.call is not None and event.call.name == "browser_click":
            os._exit(73)

    if initial:
        base = build_turn_snapshot(session)
        prepared = execute_turn(session, [ToolCall("browser_navigate", {"url": URL}), ToolCall("browser_snapshot", {})],
            snapshot=replace(base, tool_names=(*base.tool_names, "browser_navigate", "browser_snapshot")),
            run_id="r", session_id="s", effect_scope="fixture", turn=1, research_tools=tools)[-1]
        assert prepared.ok, prepared.model_text
        elements = decode_json_object(prepared.canonical["value"])["elements"]
        assert isinstance(elements, list)
        ref = next(str(row["ref"]) for row in elements if isinstance(row, dict) and row["role"] == "button")
        snapshot = offer_browser_tools(base, session)
        raw = json.dumps({"tool": "browser_click", "args": {"ref": ref, "purpose": "result"}})
        (state / "request.json").write_text(json.dumps({"snapshot": snapshot_description(snapshot), "proposal": raw}), encoding="utf8")
    else:
        saved = parse_json_object((state / "request.json").read_text(encoding="utf8"))
        snapshot = restore_snapshot(saved["snapshot"])
        raw = str(saved["proposal"])
    plan = normalize_turn(decode_text_turn(raw), snapshot=snapshot)
    assert not plan.protocol_error, plan
    parent_call = plan.calls[0]
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="task",
        turn=2, research_tools=tools, intent_sink=sink, managed_outputs=sink.managed_outputs,
        session_id="s", durable=sink.durable, on_event=stop_after_click if initial else None)

    def dispatch() -> ToolResult:
        return execute_turn(session, [parent_call], snapshot=snapshot, run_id="r", session_id="s",
            effect_scope="task", turn=2, research_tools=tools, executors={"codemode": parent},
            intent_sink=sink, managed_outputs=sink.managed_outputs, durable=sink.durable)[0]

    if stage == "before":
        original_click = browser.click

        def interrupt(ref: str, *, query: bool = False) -> BrowserToolOutput:
            original_click(ref, query=query)
            os._exit(74)

        with patch.object(browser, "click", interrupt):
            dispatch()
        raise AssertionError("expected process exit")
    result = dispatch()
    assert not initial
    reads = [row for row in session._memory_results.values() if row.call.name == "browser_read" and row.ok]
    (state / "result.json").write_text(json.dumps({"ok": result.ok, "text": result.model_text,
        "linked": len([row for row in reads if "query_click" in row.audit])}), encoding="utf8")
    sink.durable.close()


@pytest.mark.parametrize("stage", ["before", "after"])
def test_query_restart_uses_committed_click_or_preserves_uncertainty(tmp_path: Path, stage: str) -> None:
    state = tmp_path / "state"
    with cdp_browser(tmp_path / "profile") as port:
        source = ("from pathlib import Path; import sys; "
                  "from tests.test_browser_followups_production import query_restart_probe; "
                  "query_restart_probe(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])")
        command = [sys.executable, "-c", source, str(state), str(port)]
        interrupted = subprocess.run([*command, stage], capture_output=True, text=True, timeout=60)
        assert interrupted.returncode == (74 if stage == "before" else 73), interrupted.stdout + interrupted.stderr
        resumed = subprocess.run([*command, "resume"], capture_output=True, text=True, timeout=60)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        projected = parse_json_object((state / "result.json").read_text(encoding="utf8"))
        assert projected["linked"] == (1 if stage == "after" else 0), projected
        assert projected["ok"] is (stage == "after"), projected
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = next(item for item in browser.contexts[0].pages if item.url == URL)
            assert page.locator("button").get_attribute("data-count") == "1"
