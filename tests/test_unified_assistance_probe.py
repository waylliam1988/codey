"""Script the provider boundary; keep question, task, source and Chromium real."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.completion_explanation_ab import Target
from tools.unified_assistance_probe import run_assistance


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("purpose", ["controls", "result"])
def test_answer_resumes_the_original_durable_task_and_requires_fresh_record_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, purpose: str,
) -> None:
    def replies() -> Iterator[JsonObject]:
        yield {"tool": "ask_user", "args": {"question": "请在原页登录后继续。", "options": ["已处理", "取消"]}}
        yield {"tool": "browser_snapshot", "args": {}}
        yield {"tool": "browser_read", "args": {}}
        yield {"tool": "browser_fill", "args": {"ref": {"role": "textbox", "name": "Search records"}, "value": "福州"}}
        yield {"tool": "browser_click", "args": {"ref": {"role": "button", "name": "Search"}, "purpose": purpose}}
        if purpose == "result":
            yield {"tool": "done", "args": {"summary": "尚未打开实际记录。"}}
            yield {"tool": "browser_snapshot", "args": {}}
        yield {"tool": "browser_click", "args": {"ref": {"role": "link", "name": "福州鱼丸记录"}, "purpose": "action"}}
        yield {"tool": "browser_read", "args": {}}
        yield {"tool": "done", "args": {"summary": "福州鱼丸净重为 173 克，校验码 QUARTZ_851。[e2]"}}

    install_http(monkeypatch, replies())
    summary = run_assistance(tmp_path / "assistance", Target("http://scripted.test/v1", "fixture", 123456789, 10))
    assert summary["one_durable_task"] is True, summary
    assert summary["natural_task_closed"] is True, summary
    tools = summary["tools"]
    assert isinstance(tools, list)
    assert sum(isinstance(row, dict) and row["tool"] == "browser_click" for row in tools) == 2
