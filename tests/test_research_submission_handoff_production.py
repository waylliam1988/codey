"""Exercise the frozen production Research contract against browser receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object
from tools import browser_prerequisite_ab as fixture
from tools import browser_production_live_gate as gate
from tools.completion_explanation_ab import ExperimentProvider, Target


class NativeExperimentProvider(ExperimentProvider):
    def __init__(self, target: Target, directory: Path, *, fixed_acquisition: bool = True,
                 acquisition_action: JsonObject | None = None, stream: bool = False) -> None:
        super().__init__(target, directory, fixed_acquisition=fixed_acquisition,
                         acquisition_action=acquisition_action, stream=stream)
        self.native_tools = True


def install_http(monkeypatch: pytest.MonkeyPatch, replies: Iterator[JsonObject], *, native: bool) -> None:
    call_id = 0

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> JsonObject:
        nonlocal call_id
        observe = kwargs["observe"]
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        reply = next(replies)
        message: JsonObject = {"role": "assistant", "content": json.dumps(reply), "tool_calls": []}
        if native:
            call_id += 1
            message.update(content=None, tool_calls=[{"id": f"call-{call_id}", "type": "function",
                "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}])
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if native else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    monkeypatch.setattr("tools.completion_explanation_ab.service_state", lambda _url: {})


def requests(directory: Path) -> list[JsonObject]:
    rows = [decode_json_object(json.loads(line))
            for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    return [decode_json_object(row["payload"]) for row in rows if row.get("phase") == "request"]


def assert_collecting(directory: Path) -> None:
    request = requests(directory)[-1]
    assert '"codemode"' in json.dumps(request.get("response_format", request.get("tools")))
    assert "post-wait page evidence is ready" not in json.dumps(request["messages"])


def assert_submitting(directory: Path) -> str:
    request = requests(directory)[-1]
    schema = json.dumps(request.get("response_format", request.get("tools")))
    assert "submit_research_answer" in schema and '"codemode"' not in schema
    messages = json.dumps(request["messages"], ensure_ascii=False)
    assert "QUARTZ_851" in messages
    handoff = re.search(r"post-wait page evidence is ready\. Use submit_research_answer and cite \[(e\d+)\]", messages)
    assert handoff is not None
    return handoff[1]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize(("case", "native"), [
    ("ready", False), ("early_read", False), ("partial_read", False), ("reload", False),
    ("old_citation", False), ("lost_on_submit", False), ("undeclared", False),
    ("ready", True), ("old_citation", True), ("lost_on_submit", True),
])
def test_production_handoff_requires_citable_read_on_current_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)
    if case == "lost_on_submit":
        original = ExecutionDelegate._submit_research_answer
        reloaded = False

        def submit(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
            nonlocal reloaded
            outcome = original(delegate, call, tools)
            if outcome[1] and not reloaded:
                assert delegate.browser_session is not None and delegate.browser_session.navigate(gate.URL).ok
                reloaded = True
            return outcome

        monkeypatch.setattr(ExecutionDelegate, "_submit_research_answer", submit)

    def replies() -> Iterator[JsonObject]:
        assert_collecting(directory)
        code = fixture.ROUND
        if case == "early_read":
            code = fixture.NAVIGATE + fixture.LOAD + f"const old={fixture.READ};" + fixture.WAIT + "return old;"
        elif case == "partial_read":
            code = fixture.ROUND.replace(fixture.READ, "unwrap(await tools.browser_read({limit:32}))")
        elif case == "reload":
            code = fixture.ROUND.replace(f"return {fixture.READ};", f"const old={fixture.READ};")
            code += fixture.NAVIGATE + "return old;"
        elif case == "old_citation":
            code = fixture.NAVIGATE + f"const old={fixture.READ};" + fixture.LOAD + fixture.WAIT + f"return {fixture.READ};"
        yield fixture.code_action(code)
        if case in {"early_read", "partial_read", "reload"}:
            assert_collecting(directory)
            recovery = fixture.LOAD + fixture.WAIT if case == "reload" else ""
            yield fixture.code_action(recovery + f"return {fixture.READ};")
        if case == "undeclared":
            assert_collecting(directory)
            yield fixture.submission("e2", current=True)
            return
        label = assert_submitting(directory)
        if case == "old_citation":
            assert label != "e1"
            yield fixture.submission("e1", current=False)
            assert assert_submitting(directory) == label
        if case == "lost_on_submit":
            yield fixture.submission(label, current=True)
            assert_collecting(directory)
            yield fixture.code_action(fixture.LOAD + fixture.WAIT + f"return {fixture.READ};")
            label = assert_submitting(directory)
        yield fixture.submission(label, current=True)

    install_http(monkeypatch, replies(), native=native)
    result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                           strict_research=True, research_sequence=case != "undeclared")
    assert result["passed"] is True and result["stop_reason"] == "done", result
