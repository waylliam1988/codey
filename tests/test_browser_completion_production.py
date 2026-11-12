"""Explicit browser prerequisites through the production headless completion chain."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.app.headless_runner import HeadlessRequest, HeadlessResult, run_headless
from codey.policies.browser_requirement import require_browser_research_sequence
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.policies.tool_requirement import required_tool_names
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_prerequisite_ab as fixture
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import ExperimentProvider


def add_requirement(monkeypatch: pytest.MonkeyPatch, declaration: str) -> None:
    def execute(request: HeadlessRequest, *, emit_jsonl: Callable[[dict[str, object]], None],
                connect_provider: Callable[[str], ExperimentProvider]) -> HeadlessResult:
        return run_headless(replace(request, required_tools=(*request.required_tools, declaration)),
                            emit_jsonl=emit_jsonl, connect_provider=connect_provider)

    monkeypatch.setattr(gate, "run_headless", execute)


def request_messages(directory: Path) -> list[str]:
    return [json.dumps(row["payload"]["messages"], ensure_ascii=False)
            for row in map(json.loads, (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines())
            if row.get("phase") == "request"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("case", "next_step"), [
    ("missing_wait", "browser_wait_missing"), ("wrong_wait", "browser_wait_missing"),
    ("old_read", "browser_read_after_wait_missing"), ("other_tab", "browser_wait_missing"),
    ("old_citation", "browser_post_wait_citation_missing"), ("mixed_citation", "browser_post_wait_citation_missing"),
])
def test_research_requires_the_declared_wait_read_and_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, next_step: str,
) -> None:
    add_requirement(monkeypatch, require_browser_research_sequence(url=gate.URL, condition="text", value="鱼丸"))
    monkeypatch.setattr(gate, "HTML", gate.HTML.replace("尚未加载记录。请填写 Locality 并点击 Load record。",
        "旧缓存：福州样本净重 101 克，校验码 OLD_101。请填写 Locality 并点击 Load record。"))
    replies = fixture.prefix("old_citation" if case == "mixed_citation" else case)
    if case == "mixed_citation":
        replies[1] = {"tool": "submit_research_answer", "args": {"state": "answered",
            "markdown": "福州页面出现了两份记录。旧记录 [e1] 和新记录 [e2]。"}}
    if next_step == "browser_wait_missing":
        replies.append(fixture.code_action(fixture.WAIT + f"return {fixture.READ};"))
    elif next_step == "browser_read_after_wait_missing":
        replies.append(fixture.code_action(f"return {fixture.READ};"))
    replies.append(fixture.submission("e2", current=True))
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = gate.run_case(directory, "research", TARGET, strict_research=True, max_turns=6)
    assert row["passed"] and row["stop_reason"] == "done", row
    messages = request_messages(directory)
    assert len(messages) == len(replies)
    assert next_step in messages[2]
    if next_step == "browser_post_wait_citation_missing":
        assert "[e2] for the required page" in messages[2]
    receipts = tool_receipts(directory / "state")
    waits = [r.result for r in receipts if r.result.call.name == "browser_wait" and r.result.ok]
    reads = [r.result for r in receipts if r.result.call.name == "browser_read" and r.result.ok]
    assert waits[-1].audit["browser_document"] == reads[-1].audit["browser_document"]
    if case == "other_tab":
        assert waits[0].audit["browser_document"] != reads[0].audit["browser_document"]
    if case in {"old_citation", "mixed_citation"}:
        assert len(reads) == 2
    decisions = [value for _, body in artifacts(directory / "state")
                 if isinstance(value := body.get("data"), dict) and value.get("purpose") == "done"]
    assert any("required_tools_satisfied" in json.dumps(decision) for decision in decisions)


def test_research_sequence_is_explicit_and_frozen_without_granting_access() -> None:
    declaration = require_browser_research_sequence(url=gate.URL, condition="text", value="鱼丸")
    submission = SimpleNamespace(project=None, strict_research=True, required_tools=(declaration,))
    policy = build_task_policy(submission, task_kind="research")
    assert declaration in policy.required_checks
    assert TaskPolicy.from_payload(policy.to_payload()) == policy
    assert not policy.allows("browser.observe")
    with pytest.raises(ValueError, match="requires strict Research"):
        build_task_policy(submission, task_kind="chat")
    with pytest.raises(ValueError, match="invalid tool predicate"):
        required_tool_names([declaration + " "])
    with pytest.raises(ValueError, match="condition"):
        require_browser_research_sequence(url=gate.URL, condition="invalid", value="鱼丸")
