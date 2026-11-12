from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import required_source_prefetch_ab as probe
from tools.browser_production_live_gate import SOURCE_SUCCESS_MARKER, URL, run_case
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import Target


def test_requires_one_explicit_authorized_unattempted_url_and_frozen_tools() -> None:
    policy = TaskPolicy(frozenset({"control", "web.read", "browser.observe"}), sources_open_required=True)
    session = TaskSession(policy, task_kind="chat", required_source_urls=frozenset({URL}))
    snapshot = build_turn_snapshot(session)
    assert probe.eligible_source(session, snapshot) == URL
    assert probe.eligible_source(session, replace(snapshot, nested_tool_names=())) is None
    assert probe.eligible_source(session, replace(snapshot, tool_names=())) is None
    for denied in (replace(policy, grants=frozenset({"control"})), replace(policy, sources_open_required=False)):
        assert probe.eligible_source(replace(session, policy=denied), snapshot) is None
    for urls in (frozenset(), frozenset({URL, "https://example.com/another"}), frozenset({"http://127.0.0.1/"})):
        assert probe.eligible_source(replace(session, required_source_urls=urls), snapshot) is None
    assert probe.eligible_source(replace(session, opened_sources={URL}), snapshot) is None


def test_scheduling_runs_once_and_does_not_repeat_an_existing_attempt() -> None:
    schedule = probe.first_attempt(lambda: URL)
    action = schedule([])
    assert action is not None and action["tool"] == "codemode"
    assert schedule([]) is None
    receipt = EffectReceipt("previous", ToolResult(ToolCall("open_url", {"url": URL}), "failed", ok=False), None)
    assert probe.first_attempt(lambda: URL)([receipt]) is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("status", [200, 403])
def test_first_request_delivery_and_real_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, status: int,
) -> None:
    summary = f"实际记录：{SOURCE_SUCCESS_MARKER}" if status == 200 else "来源返回 HTTP 403，无法读取正文。"
    done: JsonObject = {"tool": "done", "args": {"summary": summary}}
    open_source: JsonObject = {"tool": "codemode", "args": {
        "code": "return await tools.open_url({url:inputs.url});", "inputs": {"url": URL},
    }}
    replies = [done] if arm == "B" else [open_source, done] if status == 200 else [done] * 4
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    target = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)
    row = probe.run_arm(directory, arm, status, target, max_turns=4)
    if status == 403 and arm == "A":
        assert row["passed"] is False and row["source_attempts"] == 0
        assert row["stop_reason"] == "no_progress"
        return
    assert row["passed"] is True, row
    assert row["stop_reason"] == ("done" if status == 200 else "blocked")
    assert row["source_attempts"] == 1 and row["source_results_delivered"] is True
    assert row["first_request_has_source_result"] is (arm == "B")
    assert row["runtime_actions"] == (1 if arm == "B" else 0)
    assert row["model_calls"] == (1 if arm == "B" else 2)
    source, = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "open_url"]
    assert source.call.args["url"] == URL and source.ok is (status == 200)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize(("status", "multiple", "newline"), [
    (200, False, False), (403, False, False), (403, True, False), (200, False, True), (403, False, True),
])
def test_production_opens_required_source_before_first_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, multiple: bool, newline: bool,
) -> None:
    expected = SOURCE_SUCCESS_MARKER if status == 200 else "HTTP 403"
    install_http(monkeypatch, [{"tool": "done", "args": {"summary": f"Source result: {expected}"}}])
    directory = tmp_path / "production"
    target = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)
    other = "https://example.com/second-required-source"

    def task_with_layout(task: str) -> str:
        if newline:
            return f"帮我看看这个页面\n{URL}\n请报告正文记录或访问失败的实际原因。"
        return task + (f" Also open {other}." if multiple else "")

    row = run_case(directory, "chat", target, max_turns=1, source_failure=True, source_attempt_only=True,
                   source_status=status, task_transform=task_with_layout)
    assert row["passed"] is True, row
    sources = [r for r in tool_receipts(directory / "state") if r.result.call.name == "open_url"]
    assert [source.result.call.args["url"] for source in sources] == ([URL, other] if multiple else [URL])
    transcript = (directory / "provider.jsonl").read_text(encoding="utf-8")
    request, = [line for line in transcript.splitlines() if '"phase": "request"' in line]
    assert expected in request
    for index, source in enumerate(sources):
        assert source.result.ok is (status == 200) and source.result.call.call_id == f"required-source:{index}"
        assert source.identity in request
