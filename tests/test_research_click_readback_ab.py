"""Natural browser Research with real clicks, record projections and citations."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import research_click_readback_ab as probe
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def answer(text: str, *, state: str = "answered") -> JsonObject:
    return {"tool": "submit_research_answer", "args": {"state": state, "markdown": text}}


def click(label: str, continuation: str = "") -> JsonObject:
    return {"tool": "codemode", "args": {"code": "const s=unwrap(await tools.browser_snapshot({}));"
        + ("" if continuation else "return ") + "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name===inputs.label).ref}));"
        + continuation, "inputs": {"label": label, "url": probe.CASES["missing"].url}}}


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("name", ["visible", "interactive", "missing", "timeout", "ambiguous", "table", "already_read", "reload"])
def test_research_click_readback_and_record_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    case = probe.CASES[name] if name in probe.CASES else probe.CASES["missing"]
    if name == "timeout":
        original = probe.CASES["interactive"]
        case = replace(original, html=original.html.replace("},850)", "},60000)"))
    elif name == "ambiguous":
        case = replace(case, html=case.html.replace("A-174", "A-173"))
    elif name == "table":
        case = replace(case, html=("<table><tr><td>A-173</td><td>运输中</td><td>周五 18:20</td></tr>"
                                  "<tr><td>A-174</td><td>周四 12:10</td></tr></table>"))
    if name in {"interactive", "missing"}:
        replies = [click("查实时记录" if name == "interactive" else "查看配送详情"),
                   answer(f"网页显示：{'，'.join(case.facts)}。 [e2]")]
    elif name == "already_read":
        replies = [click("查看配送详情", "return unwrap(await tools.browser_read({}));"),
                   answer(f"网页显示：{'，'.join(case.facts)}。 [e2]")]
    elif name == "reload":
        replies = [click("查看配送详情", "return unwrap(await tools.browser_navigate({url:inputs.url}));"),
                   answer("预计送达时间已确认：周五 18:20。 [e1]"),
                   answer("订单正在运输中，预计送达时间还没有确认。 [e1]", state="partial")]
    elif name == "timeout":
        replies = [click("查实时记录"), answer("B-19 已入库，库位 C-42。 [e1]"),
                   answer("目前仍显示实时记录尚未加载。 [e1]", state="partial")]
    elif name == "ambiguous":
        replies = [answer("A-173 运输中，预计周四 12:10 送达。 [e1]"),
                   answer("页面有两条同编号的记录，无法确定哪个预计送达时间属于此订单。 [e1]", state="partial")]
    else:
        replies = [answer(f"页面显示：{'，'.join(case.facts)}。 [e1]")]
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = probe.run_arm(directory, case, "B", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10))
    assert row["passed"] is (name not in {"timeout", "ambiguous", "reload"}), row
    assert row["model_calls"] == len(replies)
    assert row["automatic_reads"] == (1 if name in {"interactive", "missing"} else 0)
    receipts = [item.result for item in tool_receipts(directory / "state")]
    if name in {"missing", "table"}:
        assert "周四 12:10" not in str(row["scoped_texts"])
        assert "A-174" not in str(row["scoped_texts"])
    if name in {"ambiguous", "timeout", "reload"}:
        submissions = [result for result in receipts if result.call.name == "submit_research_answer"]
        assert [result.ok for result in submissions] == [False, True]
    if name == "ambiguous":
        assert row["scoped_reads"] == 0
    if name == "timeout":
        assert "no confirmed updated text" in (directory / "provider.jsonl").read_text(encoding="utf-8")
    if name == "already_read":
        assert row["browser_reads"] == 1
        assert '"already_read": true' in (directory / "readback.jsonl").read_text(encoding="utf-8")
    if name in {"interactive", "missing"}:
        selected, = [result for result in receipts if result.call.name == "browser_read" and result.audit.get("readback_of")]
        assert selected.audit["record_id"] == probe.record_id(case.question)
        assert "[e2]" in str(row["answer"])
