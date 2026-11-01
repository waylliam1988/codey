"""Domain feedback observes repetition without deciding absence or weakening gates."""

import json

import pytest

from codey.operations.kernel_prompt import _repair_prompt
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.task_policy import TaskPolicy
from codey.research.action_feedback import repeat_observation
from codey.research.context_view import local_research_context
from codey.research.local_scope import current_scope, effective_answers, registered_plan
from codey.runtime.core.models import ToolCall, ToolPlan
from tests.test_research_findings import URL, execute
from tests.test_research_inline_submission import inline_setup
from tests.test_research_local_scope import local_setup


@pytest.mark.parametrize("native", [False, True])
def test_changed_queries_same_result_get_only_declared_exits_and_never_auto_close(tmp_path, native):
    store, tools, session, delegate = local_setup(tmp_path)
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    try:
        for index, query in enumerate(("missing first", "missing second")):
            result = execute(delegate, tools, session, "knowledge_search", {"query": query})
            session._memory_results[str(index)] = result
        session.model_tool_names = ("submit_research_answer",)
        frozen = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        scope = frozen.research_scope
        observation = repeat_observation(session, scope, frozen.tool_names)
        assert observation and "same search result" in observation["observation"]
        assert all(row.startswith("submit_research_answer") for row in observation["available_exits"])
        assert "does not prove an answer is absent" in observation["instruction"]
        body = local_research_context(session, tools.ledger, store, scope, tool_names=frozen.tool_names)
        assert '"repeated_action"' in body
        assert not effective_answers(session, registered_plan(session))
        assert not tools.created_ids and len(tools.ledger.investigations) == 2
        # The model can still explicitly declare a real gap without a placeholder.
        result, ok, _ = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)._execute_research(
            ToolCall("submit_research_answer", {"state": "unresolved", "findings": []}))
        assert ok and not result.canonical["research_answer"]["findings"]
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_inline_form_repairs_use_actual_frozen_description_and_example(tmp_path, native):
    store, tools, session, _ = inline_setup(tmp_path)
    try:
        frozen = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        spec = next(row for row in frozen.model_specs if row.name == "submit_research_answer")
        repair = _repair_prompt(ToolPlan(calls=[], control=None, protocol_error="findings[0] must be a string",
                                         protocol_tool_name=spec.name), snapshot=frozen, native=native)
        assert spec.description in repair and spec.json_examples[0] in repair
        assert "ONE object" in repair and "never new answer objects" in repair
        assert "Call done" not in repair and not tools.created_ids
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_version_failure_returns_frozen_original_locators_and_still_requires_explicit_repair(tmp_path):
    store, tools, session, _ = inline_setup(tmp_path)
    try:
        tools.search.text = "First documented version: 3.7.0. " + "x" * 1100 + "\n\nSecond documented version: 3.22.0."
        session._memory_results["versions"] = execute(ExecutionDelegate(session=session, research_tools=tools),
            tools, session, "open_url", {"url": URL})
        scope = current_scope(session)
        passages = session._memory_results["versions"].canonical["read_passages"]
        first = next(row["ref"] for row in passages if "3.7.0" in tools.ledger.read_passages[row["ref"]].excerpt)
        second = next(row["ref"] for row in passages if "3.22.0" in tools.ledger.read_passages[row["ref"]].excerpt)
        assert first != second
        delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
        finding = {"claim": "The documented release versions are 3.7.0 and 3.22.0.", "passage_ref": first}
        rejected, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", {"state": "answered", "finding": finding}))
        assert not ok and "available_version_locators" in rejected.model_text
        anchors = json.loads(rejected.model_text.split("\n")[-1])["available_version_locators"]["original_mentions"]
        assert any(row["passage_ref"] == second and row["literal"] == "3.22.0" for row in anchors)
        assert not tools.created_ids and not tools.ledger.references
        accepted, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", {
            "state": "answered", "finding": {**finding, "additional_passage_refs": [second]}}))
        assert ok and accepted.canonical["research_answer"]["state"] == "answered"
    finally:
        store.close()
