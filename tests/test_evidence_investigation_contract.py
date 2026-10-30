"""Evidence-only investigations cannot inherit their parent's report contract."""

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("strict", [False, True])
def test_investigation_done_accepts_only_acquisition_receipt(native, strict):
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=strict),
                          task_kind="evidence_followup")
    snapshot = build_turn_snapshot(session, native=native)
    spec = next(item for item in snapshot.frozen_specs if item.name == "done")
    assert set(dict(spec.parameters)) == {"summary"}
    assert "parent task owns the final report" in spec.description
    assert "report_sections" not in snapshot.contract_text
    for extra, accepted in [({}, True), ({"report_sections": {"evidence": ["old report"]}}, False)]:
        args = {"summary": "Saved one finding; independent replication remains unlocated.", **extra}
        plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done-1", "done", args),)), snapshot=snapshot)
        assert bool(plan.protocol_error) is not accepted


def test_parent_research_retains_its_full_report_contract():
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research")
    spec = next(item for item in build_turn_snapshot(session).frozen_specs if item.name == "done")
    assert "report_sections" in dict(spec.parameters)
    assert set(spec.required) == {"summary", "report_sections"}


def test_investigation_cannot_end_by_repeating_parent_without_attempting_acquisition():
    from codey.operations.completion_gate import evaluate

    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="evidence_followup",
                          research_acquisition_limits=(2, 2))
    rejected = evaluate(session, "The parent saved a finding; other comparisons remain unknown.")
    assert not rejected.complete
    assert "has not attempted acquisition" in rejected.followup
    # Failed real acquisition is still an honest attempt, not a requirement
    # to fabricate a new finding or to make an unavailable source succeed.
    session._memory_results["failed-search"] = ToolResult(
        ToolCall("source_search", {"url": "https://example.com/study", "query": "outcome"}),
        ok=False, model_text="source unavailable")
    accepted = evaluate(session, "Source search failed; outcome comparison remains unverified.")
    assert accepted.complete
    assert any(row.check_id == "research_investigation_attempted" and row.status == "pass"
               for row in accepted.proof.checks)


def test_phase_method_guidance_does_not_request_forbidden_done_arguments():
    from codey.research.methods import research_method_guidance

    assert "knowledge_refs" not in research_method_guidance("中文治疗研究", evidence_only=True)
    assert "knowledge_refs" in research_method_guidance("中文治疗研究")
