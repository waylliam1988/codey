"""A current-run note ref needs removal, not another read/copy loop."""

from codey.completion.contract import CHECK_FAIL, CompletionCheck
from codey.operations.research_completion_checks import research_completion_feedback
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import EvidenceItem, ResearchLedger


def test_current_note_repair_is_field_local_and_preserves_earlier_refs(monkeypatch):
    ledger = ResearchLedger()
    ledger.evidence_items.append(EvidenceItem(claim="finding", source_url="https://example.org/",
        excerpt="result", note_id="new-note"))
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research")
    session.last_done_args = {"knowledge_refs": [
        {"id": "earlier-object", "revision": 2}, {"id": "new-note", "revision": 99}]}
    before = repr(session.last_done_args)
    checks = [CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid")]
    text = research_completion_feedback(session, "draft", {"research_ledger": ledger}, checks)
    assert "done.knowledge_refs[1]" in text and "done.knowledge_refs[0]" not in text
    assert "Remove these entries" in text and "No knowledge_read or new write is needed" in text
    assert "Next action: execute one knowledge_read" not in text
    assert repr(session.last_done_args) == before  # No silent normalization or acceptance.
    from codey.operations import completion_gate

    # Check the composed feedback too: generic gate text must not reintroduce
    # the instruction to read notes before the field-local repair.
    monkeypatch.setattr(completion_gate, "strict_research_checks", lambda *a, **k: [])
    verdict = completion_gate.evaluate(session, "draft", context={"research_ledger": ledger})
    assert not verdict.complete
    assert "Remove these entries" in verdict.followup
    assert "Read the source note to find" not in verdict.followup
    session.task_kind = "hybrid"
    text = research_completion_feedback(session, "draft", {"research_ledger": ledger}, checks)
    assert "Remove these entries" not in text
    assert "Next action: execute one knowledge_read" in text
