"""Exercise native detail choices through production Research and its receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.automation.browser_detail import DetailControl, observe_details
from codey.automation.browser_session import BrowserSession, _browser_call
from codey.policies.tool_requirement import _browser_body
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.types.json_value import JsonObject
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools import research_natural_handoff_ab as natural
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Arm, Target

CASES = {
    "warehouse": replace(natural.CASES["interactive"], html=(
        '<section aria-label="批次 B-19"><h2>批次 B-19</h2><p>库存查询，详细记录可以展开。</p>'
        '<details ontoggle="window.siteAction = true"><summary>查实时记录</summary>'
        '<p>状态：已入库。库位：C-42。</p><p hidden>HIDDEN_VALUE</p></details></section>')),
    "orders": replace(natural.CASES["missing"], html=(
        '<article><h2>订单 A-173</h2><p>当前状态：运输中</p>'
        '<details><summary>查看配送详情</summary><p>预计送达：周五 18:20。</p></details></article>'
        '<article><h2>订单 A-174</h2><p>其他订单的进度</p>'
        '<details><summary>查看配送详情</summary><p>预计送达：周四 12:10。</p></details></article>')),
    "visible": replace(natural.CASES["visible"], html=(
        '<section aria-label="馆藏 LY-72">' + natural.CASES["visible"].html
        + '<details><summary>查看馆藏详情</summary><p>馆藏位于一层。</p></details></section>')),
}


def run_case(directory: Path, case: natural.Case, target: Target) -> JsonObject:
    def unmodified(_directory: Path, _arm: Arm) -> nullcontext[None]:
        return nullcontext()

    with patch.object(natural, "experiment", unmodified):
        return natural.run_arm(directory, case, "A", target)


def submit(markdown: str, *, state: str = "answered") -> JsonObject:
    return {"tool": "submit_research_answer", "args": {"markdown": markdown, "state": state}}


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("scenario", "native"), [
    ("warehouse", False), ("orders", True), ("visible", False), ("reload", False),
    ("ambiguous", False), ("same_name", False), ("scripted", False), ("wrong_record", False),
])
def test_production_details_are_optional_record_bound_and_citable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, native: bool,
) -> None:
    directory = tmp_path / "run"
    case = CASES.get(scenario, CASES["warehouse"])
    if scenario == "ambiguous":
        case = replace(case, html=case.html + case.html)
    elif scenario == "same_name":
        case = replace(case, html=case.html.replace("</section>",
            "<details><summary>查实时记录</summary><p>另一处同名详情</p></details></section>"))
    elif scenario == "scripted":
        case = replace(case, html=natural.CASES["interactive"].html)
    elif scenario == "wrong_record":
        case = replace(case, html=case.html.replace("B-19", "B-190"))
    if native:
        monkeypatch.setattr(natural, "ExperimentProvider", NativeExperimentProvider)
    browser: BrowserSession | None = None
    def observe(active: BrowserSession, document: JsonObject, task: str) -> tuple[DetailControl, ...]:
        nonlocal browser
        browser = active
        return observe_details(active, document, task)

    monkeypatch.setattr("codey.operations.browser_detail.observe_details", observe)

    def replies() -> Iterator[JsonObject]:
        first = requests(directory)[0]
        schema = json.dumps(first.get("response_format", first.get("tools")))
        messages = json.dumps(first["messages"], ensure_ascii=False)
        assert "submit_research_answer" in schema and "codemode" in schema
        if scenario in {"ambiguous", "same_name", "scripted", "wrong_record"}:
            assert PAGE_DETAIL_TOOL not in schema
            yield submit("目前无法确认实时状态和库位。 [e1]", state="partial")
            return
        choices = set(re.findall(r"detail-[a-f0-9]{12}", messages))
        assert PAGE_DETAIL_TOOL in schema and len(choices) == 1
        assert "C-42" not in messages and "周五 18:20" not in messages and "周四 12:10" not in messages
        if scenario == "visible":
            yield submit("LY-72 还有 6 本可借，19:40 闭馆。 [e1]")
            return
        if scenario == "reload":
            assert browser is not None and browser.navigate(case.url).ok
        yield {"tool": PAGE_DETAIL_TOOL, "args": {"choice": choices.pop()}}
        following = requests(directory)[-1]
        current = json.dumps(following["messages"], ensure_ascii=False)
        if scenario == "reload":
            assert "stale or no longer readable" in current
            yield submit("当前未确认状态和库位。 [e1]", state="partial")
        else:
            assert all(fact in current for fact in case.facts)
            assert "HIDDEN_VALUE" not in current and "周四 12:10" not in current
            assert browser is not None
            assert _browser_call(lambda: browser._page_on_browser_thread().evaluate(
                "() => !window.siteAction && !document.querySelector('details').open")) is True
            yield submit("、".join(case.facts) + "。 [e2]")

    install_http(monkeypatch, replies(), native=native)
    row = run_case(directory, case, Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10))
    assert row["stop_reason"] == "done", row
    receipts = [item.result for item in tool_receipts(directory / "state")]
    details = [result for result in receipts if result.call.name == PAGE_DETAIL_TOOL]
    assert [result.ok for result in details] == (
        [True] if scenario in {"warehouse", "orders"} else [False] if scenario == "reload" else [])
    if scenario in {"warehouse", "orders"}:
        assert "[e2]" in str(row["answer"])
        read, = details
        assert read.audit.get("browser_document") and read.canonical.get("read_passages")
        assert all(fact in (_browser_body(read) or "") for fact in case.facts)
        assert "周四 12:10" not in (_browser_body(read) or "")
    assert not any(result.call.name == "browser_click" for result in receipts)
