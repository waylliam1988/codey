"""Receipt-bound metadata delivery through the ordinary text completion path."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.operations.browser_text_report import report_scope
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import URL, run_case
from tools.completion_explanation_ab import Target


def test_plain_browser_report_requires_a_request_to_deliver_the_metadata() -> None:
    session = TaskSession(TaskPolicy(frozenset({"browser.effect", "browser.interact"})), task_kind="chat",
        task_text="用 browser_eval 读取 window.sessionCode；用 browser_tab_list 获取 URL。不要报告 token 和 URL。")
    assert report_scope(session) is None
    session.task_text = session.task_text.replace("不要报告", "请报告")
    assert (scope := report_scope(session)) is not None and scope.expression == "window.sessionCode"


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("case", ["omitted", "wrong", "stale"])
def test_plain_browser_report_uses_current_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const s=unwrap(await tools.browser_snapshot({}));"
            "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
            "const fresh=unwrap(await tools.browser_snapshot({}));"
            "unwrap(await tools.browser_click({ref:fresh.elements.find(e=>e.name==='Load record').ref}));"
            "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
            "unwrap(await tools.browser_read({}));"
            "unwrap(await tools.browser_eval({code:'return window.__contractToken;'}));"
            "return unwrap(await tools.browser_tab_list({}));")
    acquisition: JsonObject = {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}}
    prose = "实际净重是 173 克，校验码是 QUARTZ_851。"
    wrong_url = URL.replace("browser-record", "browser_record")
    draft = prose if case != "wrong" else f"{prose} 页面见 {wrong_url}。\\n脚本 token：MADE_UP_TOKEN"
    done: JsonObject = {"tool": "done", "args": {"summary": draft}}
    replies = [acquisition, done]
    if case == "stale":
        reload: JsonObject = {"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_navigate({url:inputs.url}));",
                                                          "inputs": {"url": URL}}}
        replies = [acquisition, reload, done, acquisition, done]
    install_http(monkeypatch, replies)
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("codey.app.review_service.run_review", lambda *_args, **_kwargs: None)
    result = run_case(tmp_path / "run", "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                      structured=True, max_turns=6)
    answer = str(result["answer"])
    assert result["passed"] is True and result["stop_reason"] == "done", result
    assert prose in answer and f"当前标签页 URL：{URL}" in answer and "脚本 token：SCHEMA_TOKEN_74" in answer
    assert wrong_url not in answer and "MADE_UP_TOKEN" not in answer
    if case == "stale":
        assert "current browser document" in str(decode_json_object(result["first_rejection"])["reason"])
