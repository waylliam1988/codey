"""Research continuation facts survive misleading narrative checkpoints."""

import json

from codey.operations.kernel_prompt import working_context
from codey.operations.task_guidance import task_working_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_research_observations
from codey.runtime.core.models import ToolCall, ToolResult


def test_failed_writes_and_search_hits_never_become_saved_or_opened_facts():
    session = TaskSession(policy=TaskPolicy(
        grants=frozenset({"control", "web.read", "knowledge.write"}), strict_research=True))
    session.record_search_result("r1", "https://example.com/not-opened")
    session._memory_results["failed-write"] = ToolResult(
        call=ToolCall(name="knowledge_write", args={}), ok=False,
        model_text="ERROR: excerpt missing", canonical={"evidence_items": [{"claim": "not saved"}]})
    text = task_working_context(session)
    state = json.loads(text.split("summaries):\n", 1)[1].split("\n", 1)[0])
    assert state["saved_notes"] == 0 and state["saved_evidence_count"] == 0
    assert state["opened_sources"] == {} and state["saved_findings"] == []
    assert state["recent_receipts"] == [{"result_ref": "failed-write", "tool": "knowledge_write",
                                          "ok": False, "error": "ERROR: excerpt missing"}]
    assert "Registration is not reported efficacy" in text


def test_observations_are_bounded_and_preserve_saved_claim_source_and_stance():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"}), strict_research=True))
    for n in range(20):
        session.record_open(f"https://example.com/{n}")
        session.record_evidence(f"https://example.com/{n}", "measured result")
        session._memory_results[f"receipt-{n}"] = ToolResult(
            call=ToolCall(name="knowledge_write", args={}), ok=True, model_text="saved",
            canonical={"evidence_items": [{"source_url": f"https://example.com/{n}", "excerpt": "x" * 600}],
                       "research_observation": {"evidence": [{"claim": f"claim-{n}",
                            "source_url": f"https://example.com/{n}", "excerpt": "x" * 1000, "stance": "limits"}]}})
    state = json.loads(render_research_observations(session).split("summaries):\n", 1)[1].split("\n", 1)[0])
    assert len(state["opened_sources"]) == 10 and state["saved_evidence_count"] == 20
    assert len(state["recent_receipts"]) == 4 and len(state["saved_findings"]) == 6
    assert state["saved_findings"][-1] == {"claim": "claim-19", "source_url": "https://example.com/19",
                                            "excerpt": "x" * 600, "stance": "limits"}


def test_ordinary_context_is_unchanged_and_denied_tools_are_not_requested():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    assert task_working_context(session) == working_context(session)
    session.policy = TaskPolicy(grants=frozenset({"control"}), strict_research=True)
    text = task_working_context(session)
    assert "source_search" not in text
    assert "Before drafting, save" not in text


def test_evidence_investigation_retains_observations_without_requiring_a_report():
    from codey.operations.kernel_prompt import _repair_prompt
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.runtime.core.models import ToolPlan

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "knowledge.write"})),
                          task_kind="evidence_followup")
    assert "Research observations" in task_working_context(session)
    prompt = _repair_prompt(ToolPlan(calls=[], control=None, protocol_error="invalid_args",
                                    protocol_tool_name="done"), snapshot=build_turn_snapshot(session), native=False)
    assert "complete response in the requested format" in prompt
    assert '"evidence":["supported finding [1]"]' not in prompt
