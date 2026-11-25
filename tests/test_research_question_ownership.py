"""Question ownership follows the original execution receipts through recovery."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel_facts import bind_source_identity, project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.questions import INVESTIGATION_TOOLS
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, parse_json_object

URL = "https://example.com/study"
TEXT = "Writers and readers can run at the same time. There is only one writer at a time."


class Search:
    def fetch(self, url: str) -> dict[str, object]:
        return {"status": "ok", "url": url, "title": "Original study", "text": TEXT, "truncated": False}

    def search(self, query: str, *, limit: int = 8) -> list[JsonObject]:
        return [{"url": URL, "title": "Original study", "snippet": "Lead, not evidence"}]


def setup(path: Path) -> tuple[KnowledgeStore, ResearchTools, TaskSession, ExecutionDelegate]:
    store = KnowledgeStore(path)
    tools = ResearchTools(Search(), store, KnowledgeChanges(path), session_id="first")
    session = TaskSession(TaskPolicy(frozenset({"web.read", "knowledge.write", "knowledge.read", "control"})),
        task_kind="research", task_text="Explain writer concurrency.")
    tools.ledger.questions = ("What limits writer concurrency?",)
    session.research_questions = tools.ledger.questions
    return store, tools, session, ExecutionDelegate(session=session, research_tools=tools)


def execute(delegate: ExecutionDelegate, tools: ResearchTools, session: TaskSession, name: str, args: JsonObject) -> ToolResult:
    from codey.research.questions import QUESTION_BOUND_TOOLS

    call = ToolCall(name, {"question_ref": "q1", **args} if name in QUESTION_BOUND_TOOLS and tools.ledger.questions else args)
    result, ok, _ = delegate._execute_research(call)
    if name == "open_url":
        assert ok, result.model_text
    if ok:
        result = bind_source_identity(session, call, result, ok=ok)
        project_result_observation(session, call, result, ok=ok, research_ledger=tools.ledger)
    return result


def report_args(ref: str) -> JsonObject:
    return {"summary": f"Only one writer can write at a time. [{ref}]", "report_sections": {
        "evidence": [f"Only one writer can write at a time. [{ref}]"], "counter": ["本轮尚未调查反证，仍有证据缺口。"],
        "coverage": [{"question_ref": "q1", "state": "partial", "answer": "Writer concurrency answered; version boundaries remain unknown.",
            "findings": [ref], "investigations": [], "task_refs": ["r1"]}]}}


def test_source_search_alias_preserves_question_and_enforces_its_frozen_requirement() -> None:
    from codey.toolchain.tool_spec import validate_args_with_spec

    session = TaskSession(TaskPolicy(
        frozenset({"web.read", "knowledge.read", "control"}), strict_research=True))
    session.research_questions = ("Where is the restriction?",)
    session.opened_sources = {URL}
    session.source_ids = {URL: "s1"}
    snapshot = build_turn_snapshot(session)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "source_search")
    valid = {"source_id": "s1", "query": "writer", "question_ref": "q1"}
    assert not validate_args_with_spec(spec, valid)
    for bad in ({"source_id": "s1", "query": "writer"}, {**valid, "question_ref": "q9"}):
        assert validate_args_with_spec(spec, bad)
    plan = normalize_turn(decode_text_turn(json.dumps({"tool": "source_search", "args": valid})), snapshot=snapshot)
    assert not plan.protocol_error and plan.calls[0].args == valid


def test_base_tools_do_not_invent_a_question_binding() -> None:
    from codey.research.tool_contract import TOOL_CONTRACTS
    from codey.toolchain.tool_spec import spec_for_tool

    for name in INVESTIGATION_TOOLS | {"save_finding", "save_inference"}:
        if name not in TOOL_CONTRACTS:
            continue
        direct = canonical_json_object(dict(TOOL_CONTRACTS[name].input_schema))
        spec = spec_for_tool(name)
        assert spec is not None
        assert "question_ref" not in decode_json_object(direct["properties"])
        assert "question_ref" not in dict(spec.parameters)
        assert "question_ref" not in decode_json_object(parse_json_object(spec.json_examples[0])["args"])


@pytest.mark.parametrize("evidence_only", [False, True])
@pytest.mark.parametrize("name", sorted(INVESTIGATION_TOOLS | {"save_finding", "save_inference"}))
def test_strict_snapshot_requires_question_on_all_investigation_and_adoption_tools(tmp_path: Path, name: str, evidence_only: bool) -> None:
    store, tools, session, delegate = setup(tmp_path)
    try:
        session.policy = replace(session.policy, strict_research=not evidence_only)
        if evidence_only:
            session.task_kind = "evidence_followup"
            session.research_acquisition_limits = (3, 6)
        session.search_results = {"r1": URL}
        session.source_ids = {URL: "s1"}
        session.opened_sources = {URL}
        session.hit_targets = {"h1": {"url": URL, "text_hash": "", "offset": 0, "pages": ""}}
        session.evidence = [{"source_url": URL, "excerpt": "one writer"}]
        snapshot = build_turn_snapshot(session)
        if not evidence_only:
            assert "done" in snapshot.tool_names
            assert "submit_research_answer" in snapshot.tool_names
            spec = next(spec for spec in snapshot.frozen_specs if spec.name == name)
            assert "question_ref" not in dict(spec.parameters)
            assert not normalize_turn(decode_text_turn(spec.json_examples[0]), snapshot=snapshot).protocol_error
            return
        spec = next(spec for spec in snapshot.frozen_specs if spec.name == name)
        assert "question_ref" in spec.required
        done = next(spec for spec in snapshot.frozen_specs if spec.name == "done")
        assert ("report_sections" in done.required) is not evidence_only
        example = parse_json_object(spec.json_examples[0])
        arguments = decode_json_object(example["args"])
        arguments.pop("question_ref")
        example["args"] = arguments
        assert normalize_turn(decode_text_turn(json.dumps(example)), snapshot=snapshot).protocol_error
        # Execution rechecks the captured contract, rather than guessing the mode.
        from codey.operations.kernel_execution import execute_turn

        result = execute_turn(session, [ToolCall(name, arguments)], snapshot=snapshot)[0]
        assert not result.ok and "question_ref" in result.model_text
        assert not session._memory_results
    finally:
        store.close()
