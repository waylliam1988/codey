"""Experimental removals must be isolated and preserve the production owner."""

from types import SimpleNamespace

from codey.operations import kernel_prompt, knowledge_context
from codey.research import answer_contract
from tests.manual.research_workflow_ablation import ablation, seed_history


def test_rule_deduplication_keeps_current_authoritative_rules_and_task():
    session = SimpleNamespace(task_kind="research", task_text="original task", handoff="", project="")
    rules = answer_contract.ANSWER_GUIDANCE
    kwargs = {"tool_names": ("submit_research_answer",), "context_text": "evidence\n" + rules,
              "task_guidance": rules, "whole_submission": True}
    baseline = kernel_prompt.kernel_prompt_for_session(session, **kwargs)
    with ablation("dedup_rules"):
        candidate = kernel_prompt.kernel_prompt_for_session(session, **kwargs)
        assert candidate.count(rules) == 1 and baseline.count(rules) == 2
        assert "original task" in candidate and "evidence" in candidate
    assert kernel_prompt.kernel_prompt_for_session(session, **kwargs) == baseline


def test_production_answer_surface_keeps_new_knowledge_and_hides_revision():
    assert "knowledge_commit" not in answer_contract.ANSWER_TOOLS
    assert "knowledge_updates" in answer_contract.ANSWER_GUIDANCE
    assert "Research does not edit existing stored objects" in answer_contract.ANSWER_GUIDANCE


def test_recall_ablation_keeps_explicit_tools_and_restores_automatic_owner(monkeypatch):
    monkeypatch.setattr(knowledge_context, "knowledge_background", lambda *args: "prior background")
    monkeypatch.setattr(answer_contract, "whole_research", lambda session: session == "whole")
    with ablation("no_auto_recall"):
        assert knowledge_context.knowledge_background(None, "whole", "query") == ""
        assert knowledge_context.knowledge_background(None, "other", "query") == "prior background"
        assert "knowledge_search" in answer_contract.ANSWER_TOOLS
        assert "knowledge_read" in answer_contract.ANSWER_TOOLS
    assert knowledge_context.knowledge_background(None, "whole", "query") == "prior background"


def test_warm_fixture_is_persisted_and_pin_readable_after_reopen(tmp_path):
    from codey.knowledge.store import KnowledgeStore

    seed_history(tmp_path, {"documents": [{"title": "SQLite", "url": "https://www.sqlite.org/wal.html",
                                          "text": "Original read-only conditions."}]})
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        claim = store.repository.read("historical-wal-claim", 1)
        assert claim.value.status == "candidate"
        evidence_ref = claim.value.references[0]
        evidence = store.repository.read(evidence_ref.target, evidence_ref.revision)
        source_ref = evidence.value.references[0]
        assert store.repository.read(source_ref.target, source_ref.revision).value.content == evidence.value.content
        assert store.search("SQLite WAL 只读", session_id="later")
        assert not store.index._read_conn().execute("SELECT 1 FROM knowledge_versions WHERE kind='answer'").fetchall()
    finally:
        store.close()
