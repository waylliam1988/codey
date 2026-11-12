"""Check experimental Research handoff with real browser and production validation."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_prerequisite_ab as fixture
from tools import research_submission_handoff_ab as probe
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def rows(path: Path) -> list[JsonObject]:
    return [decode_json_object(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]


def latest_phase(directory: Path, expected: str) -> JsonObject:
    phase = rows(directory / "handoff.jsonl")[-1]
    assert phase["phase"] == expected, phase
    if expected == "submit":
        assert phase["tools"] == ["submit_research_answer"] and phase["nested"] == []
    elif expected == "read":
        assert phase["tools"] == ["codemode"] and phase["nested"] == ["browser_read"]
    return phase


def citation(directory: Path) -> str:
    labels = latest_phase(directory, "submit")["labels"]
    assert isinstance(labels, list) and labels and isinstance(label := labels[-1], str)
    return label


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("case", [
    "ready", "early", "partial", "reload", "answer_format", "citation_format", "citation", "old_citation", "lost_after_submit",
])
def test_handoff_and_repair_follow_current_browser_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    directory = tmp_path / "run"
    if case == "lost_after_submit":
        original = ExecutionDelegate._submit_research_answer
        reloaded = False

        def submit(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
            nonlocal reloaded
            outcome = original(delegate, call, tools)
            if outcome[1] and not reloaded:
                assert delegate.browser_session is not None and delegate.browser_session.navigate(probe.RULE.url).ok
                reloaded = True
            return outcome

        monkeypatch.setattr(ExecutionDelegate, "_submit_research_answer", submit)

    def replies() -> Iterator[JsonObject]:
        latest_phase(directory, "collect")
        if case == "early":
            yield fixture.code_action(fixture.NAVIGATE + fixture.LOAD + f"const old={fixture.READ};" + fixture.WAIT + "return old;")
        elif case == "partial":
            yield fixture.code_action(fixture.NAVIGATE + fixture.LOAD + fixture.WAIT
                                      + "return unwrap(await tools.browser_read({limit:32}));")
        elif case == "reload":
            yield fixture.code_action(fixture.ROUND.replace(f"return {fixture.READ};", f"const old={fixture.READ};")
                                      + fixture.NAVIGATE + "return old;")
        elif case == "old_citation":
            yield fixture.code_action(fixture.NAVIGATE + f"const old={fixture.READ};" + fixture.LOAD + fixture.WAIT
                                      + f"return {fixture.READ};")
        else:
            yield fixture.code_action(fixture.ROUND)
        if case in {"early", "partial"}:
            latest_phase(directory, "read")
            yield fixture.code_action(f"return {fixture.READ};")
        if case == "reload":
            phase = latest_phase(directory, "collect")
            tools = phase["tools"]
            assert phase["reason"] == "browser_wait_missing" and isinstance(tools, list) and "codemode" in tools
            yield fixture.code_action(fixture.LOAD + fixture.WAIT + f"return {fixture.READ};")
        label = citation(directory)
        if case == "answer_format":
            yield {"tool": "submit_research_answer", "args": {"state": "completed", "markdown": f"福州样本净重 173 克。 [{label}]"}}
            assert citation(directory) == label
        if case in {"citation_format", "citation"}:
            bad = "[e1-e2]" if case == "citation_format" else "[e999]"
            yield {"tool": "submit_research_answer", "args": {"state": "answered", "markdown": "福州样本净重 173 克。 " + bad}}
            assert citation(directory) == label
        if case == "old_citation":
            assert label != "e1"
            yield fixture.submission("e1", current=False)
            assert citation(directory) == label
        if case == "lost_after_submit":
            yield fixture.submission(label, current=True)
            assert latest_phase(directory, "collect")["reason"] == "browser_wait_missing"
            yield fixture.code_action(fixture.LOAD + fixture.WAIT + f"return {fixture.READ};")
            label = citation(directory)
        yield fixture.submission(label, current=True)

    install_http(monkeypatch, replies())
    result = probe.run_arm(directory, "B", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10))
    assert result["passed"] is True and result["stop_reason"] == "done", result
    receipts = [item.result for item in tool_receipts(directory / "state")]
    submissions = [result for result in receipts if result.call.name == "submit_research_answer"]
    if case in {"citation_format", "citation"}:
        assert [item.ok for item in submissions] == [False, True]
        assert result["source_reads"] == 1
    elif case == "answer_format":
        assert [item.ok for item in submissions] == [True]
        assert result["source_reads"] == 1 and result["model_calls"] == 3
    elif case == "old_citation":
        assert [item.ok for item in submissions] == [True, True]
        assert result["source_reads"] == 2 and "browser_post_wait_citation_missing" in str(result["first_rejection"])
    elif case == "lost_after_submit":
        assert [item.ok for item in submissions] == [True, True]
        assert "browser_wait_missing" in str(result["first_rejection"])
    else:
        assert len(submissions) == 1 and submissions[0].ok
    requests = [row for row in rows(directory / "provider.jsonl") if row.get("phase") == "request"]
    phases = rows(directory / "handoff.jsonl")
    assert len(requests) == len(phases)
    for phase, request in zip(phases, requests, strict=True):
        if phase["phase"] == "submit":
            payload = decode_json_object(request["payload"])
            schema = json.dumps(payload.get("response_format", payload.get("tools")))
            assert "submit_research_answer" in schema and '"codemode"' not in schema
            messages = json.dumps(payload["messages"], ensure_ascii=False)
            assert "post-wait page evidence is ready" in messages and "QUARTZ_851" in messages
