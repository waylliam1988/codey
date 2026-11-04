"""The removal experiment may change only revision exposure and its guidance."""
import json
from copy import deepcopy

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolCall, ToolResult
from tests.manual.research_no_revision_ab import READ_ONLY_GUIDANCE, RETIRED, treatment
from tests.support.legacy_research_revision_tools import REVISION_GUIDANCE
from tests.test_research_lazy_revision_ab import session


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failed_read", [False, True])
def test_candidate_removes_revision_before_and_after_read_failure_only(native, failed_read):
    s = session()
    if failed_read:
        s._memory_results["bad-read"] = ToolResult(ToolCall("knowledge_read", {"id": "bad-id"}),
                                                  "ERROR: exact id not found", ok=False)
    memory = deepcopy(s._memory_results)
    ledger, scope = ResearchLedger(), answer_scope(s)
    before = build_turn_snapshot(s, native=native)
    text = answer_context(s, ledger, scope)
    with treatment("no_revision"):
        after = build_turn_snapshot(s, native=native)
        observed = answer_context(s, ledger, scope)
    assert not RETIRED.intersection(after.tool_names)
    assert not RETIRED.intersection(before.tool_names)
    assert {spec.name: spec for spec in after.model_specs} == {spec.name: spec for spec in before.model_specs}
    assert observed.replace(READ_ONLY_GUIDANCE, "") == text.replace(REVISION_GUIDANCE, "")
    assert s._memory_results == memory
    assert build_turn_snapshot(s, native=native) == before


def test_removal_preserves_original_task_and_optional_new_knowledge_channel():
    s = session()
    s.task_text = "Read old knowledge, investigate conflicts, save new findings."
    with treatment("no_revision"):
        snapshot = build_turn_snapshot(s)
        spec = next(spec for spec in snapshot.model_specs if spec.name == "submit_research_answer")
        assert "knowledge_updates" in dict(spec.parameters)
        text = answer_context(s, ResearchLedger(), answer_scope(s))
    assert "Complete user task (verbatim):\n" + s.task_text in text
    assert "Research does not edit existing stored objects" in text
    packet = next(block for block in text.split("\n\n") if block.startswith('{"investigation_attempts":'))
    assert json.loads(packet) == {"investigation_attempts": [], "latest_error": [], "previously_read_snapshots": [],
                                 "source_locators": []}


def test_research_rejects_direct_revision_calls_without_touching_the_store(tmp_path):
    from codey.runtime.core.models import ToolCall
    from tests.test_research_findings import setup

    store, tools, session, delegate = setup(tmp_path)
    try:
        result, ok, _ = delegate._execute_research(ToolCall("knowledge_commit", {
            "commit_id": "blocked", "changes": [],
        }))
        assert not ok and not result.ok
        assert "does not modify existing knowledge objects" in result.model_text
        assert not store.repository.committed("blocked", [])
    finally:
        store.close()


def test_invalid_experimental_arm_is_not_silently_treated_as_control():
    with pytest.raises(ValueError), treatment("typo"):
        pass
