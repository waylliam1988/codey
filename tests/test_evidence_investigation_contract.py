"""Evidence-only investigations cannot inherit their parent's report contract."""

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall


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
