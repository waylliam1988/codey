"""Receipt-bound metadata delivery through the ordinary text completion path."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.operations.browser_text_report import BrowserMetadata, report_scope
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import URL, run_case
from tools.completion_explanation_ab import Target


@pytest.mark.parametrize("draft,remaining", [
    ("更多资料见 `https://example.com/help`", "更多资料见 `https://example.com/help`"),
    ("净重是 173 克。脚本 token 是 WRONG_TOKEN。更多资料见 [帮助](https://example.com/help)。",
     "净重是 173 克。更多资料见 [帮助](https://example.com/help)。"),
    ("The script token is WRONG_TOKEN. See https://example.com/help.", "See https://example.com/help."),
    ("**当前页 URL** 为 https://example.com/wrong，另见 https://example.com/help。",
     "另见 https://example.com/help。"),
    ("当前页 URL：[错误](https://example.com/wrong) 更多资料见 https://example.com/help。",
     "更多资料见 https://example.com/help。"),
    ("示例配置\n```text\nURL: https://example.com/help\n```", "示例配置\n```text\nURL: https://example.com/help\n```"),
    ("净重 173 克。\\n脚本 token：WRONG_TOKEN", "净重 173 克。"),
])
def test_metadata_report_removes_explicit_claims_and_preserves_other_links(draft: str, remaining: str) -> None:
    report = BrowserMetadata(URL, "SCHEMA_TOKEN_74")
    assert report.render(draft, chinese=True) == (
        f"{remaining}\n\n当前标签页 URL：{URL}\n脚本 token：SCHEMA_TOKEN_74")


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
    help_text = "更多资料见 `https://example.com/help`。"
    draft = prose if case != "wrong" else f"{prose} 当前页 URL 是 {wrong_url}。脚本 token 是 MADE_UP_TOKEN。{help_text}"
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
    if case == "wrong":
        assert help_text in answer
    if case == "stale":
        assert "current browser document" in str(decode_json_object(result["first_rejection"])["reason"])
