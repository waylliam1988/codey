"""Exercise the frozen production Research contract against browser receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.policies.tool_requirement import require_tool_success
from codey.providers.api_generation import AttemptObserver
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
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

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], *,
                 observe: AttemptObserver, **kwargs: object) -> JsonObject:
        nonlocal call_id
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
    def empty_service_state(_url: str) -> JsonObject:
        return {}

    monkeypatch.setattr("tools.completion_explanation_ab.service_state", empty_service_state)
    def empty_model_metadata(_url: str, _key: str) -> JsonObject:
        return {}

    monkeypatch.setattr("codey.providers.local_selection._metadata_json", empty_model_metadata)


def requests(directory: Path) -> list[JsonObject]:
    rows = [parse_json_object(line)
            for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    return [decode_json_object(row["payload"]) for row in rows if row.get("phase") == "request"]


def assert_collecting(directory: Path) -> None:
    request = requests(directory)[-1]
    assert '"codemode"' in json.dumps(request.get("response_format", request.get("tools")))
    assert "post-wait page evidence is ready" not in json.dumps(request["messages"])


def assert_submitting(directory: Path, *, sequence: bool = True) -> str:
    request = requests(directory)[-1]
    schema = json.dumps(request.get("response_format", request.get("tools")))
    assert "submit_research_answer" in schema
    assert '"codemode"' in schema  # Report readiness preserves other authorized work.
    messages = json.dumps(request["messages"], ensure_ascii=False)
    assert "QUARTZ_851" in messages
    pattern = (r"post-wait page evidence is ready\. Use submit_research_answer and cite \[(e\d+)\]" if sequence else
               r"declared wait/read obligations have formal evidence\. Use \[(e\d+)\]")
    handoff = re.search(pattern, messages)
    assert handoff is not None
    return handoff[1]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize(("case", "native"), [
    ("ready", False), ("early_read", False), ("partial_read", False), ("reload", False),
    ("old_citation", False), ("lost_on_submit", False), ("undeclared", False),
    ("ready", True), ("old_citation", True), ("lost_on_submit", True),
    ("workflow", False), ("workflow_early_read", False), ("workflow_partial_read", False),
    ("workflow_wrong_wait", False), ("workflow_later_click", False),
    ("workflow_reload", False), ("workflow_lost_on_submit", True),
])
def test_production_handoff_requires_citable_read_on_current_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)
    workflow = case.startswith("workflow")
    scenario = case.removeprefix("workflow_")
    if scenario == "lost_on_submit":
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
        if scenario == "workflow":
            code = code.replace("ref:s.elements.find(e=>e.name==='Locality').ref",
                                "ref:'['+s.elements.find(e=>e.name==='Locality').ref+']'")
            code = code.replace("ref:s.elements.find(e=>e.name==='Load record').ref",
                                "ref:'['+s.elements.find(e=>e.name==='Load record').ref+']'")
        elif scenario == "early_read":
            code = fixture.NAVIGATE + fixture.LOAD + f"const old={fixture.READ};" + fixture.WAIT + "return old;"
        elif scenario == "partial_read":
            code = fixture.ROUND.replace(fixture.READ, "unwrap(await tools.browser_read({limit:32}))")
        elif scenario == "reload":
            code = fixture.ROUND.replace(f"return {fixture.READ};", f"const old={fixture.READ};")
            code += fixture.NAVIGATE + "return old;"
        elif scenario == "old_citation":
            code = fixture.NAVIGATE + f"const old={fixture.READ};" + fixture.LOAD + fixture.WAIT + f"return {fixture.READ};"
        elif scenario == "wrong_wait":
            code = fixture.ROUND.replace("'鱼丸'", "'福州'")
        elif scenario == "later_click":
            code = fixture.ROUND.replace(f"return {fixture.READ};", f"const previous={fixture.READ};") + (
                "const after=unwrap(await tools.browser_snapshot({}));"
                "unwrap(await tools.browser_click({ref:after.elements.find(e=>e.name==='Load record').ref}));"
                "return previous;")
        yield fixture.code_action(code)
        sample = {"early_read", "partial_read", "reload", "wrong_wait", "later_click"}
        if scenario in sample:
            assert_collecting(directory)
            if scenario == "reload":
                recovery = fixture.LOAD + fixture.WAIT
            else:
                recovery = fixture.WAIT if scenario in {"wrong_wait", "later_click"} else ""
            yield fixture.code_action(recovery + f"return {fixture.READ};")
        if case == "undeclared":
            assert_collecting(directory)
            yield fixture.submission("e2", current=True)
            return
        label = assert_submitting(directory, sequence=not workflow)
        if case == "old_citation":
            assert label != "e1"
            yield fixture.submission("e1", current=False)
            assert assert_submitting(directory, sequence=not workflow) == label
        if scenario == "lost_on_submit":
            yield fixture.submission(label, current=True)
            assert_collecting(directory)
            prefix = fixture.NAVIGATE if workflow else ""
            yield fixture.code_action(prefix + fixture.LOAD + fixture.WAIT + f"return {fixture.READ};")
            label = assert_submitting(directory, sequence=not workflow)
        yield fixture.submission(label, current=True)

    install_http(monkeypatch, replies(), native=native)
    required = (require_tool_success("browser_wait", via="codemode", arguments={"condition": "text", "value": "鱼丸"}),)
    if case == "workflow_wrong_wait":
        # Use the argument-qualified requirement in place of the bare wait declaration.
        result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                               strict_research=True, explicit_wait=False, additional_required_tools=required)
    else:
        result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                               strict_research=True, research_sequence=not workflow and case != "undeclared",
                               explicit_wait=case != "undeclared")
    assert result["passed"] is True and result["stop_reason"] == "done", result
