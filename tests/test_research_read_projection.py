"""Before-save source windows survive replay without model conversation history."""

import copy

import pytest

from codey.operations.kernel_facts import project_result_observation
from codey.operations.task_session import TaskSession
from codey.research.context_view import build_research_context_view, read_evidence_projection
from codey.research.ledger import ResearchLedger
from tests.test_research_findings import URL, execute, setup

pytestmark = pytest.mark.usefixtures("public_source_dns")


def test_unsaved_passages_replay_exact_version_and_all_condition_tails(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    text = ("## Conditions\nA read only database may open if any ONE alternative holds.\n\n"
            + "Context needed for the same relationship. " * 24
            + "\n\n1. Files already exist and are readable.\n2. Files can be created.\n"
            "3. Database is immutable. These alternatives are joined by OR, not AND.\n\n"
            "## Recovery\nProgress is remembered; next invocation resumes from the recorded position.")
    tools.search.text = text
    try:
        opened = execute(delegate, tools, session, "open_url", {"url": URL})
        before = copy.deepcopy(tools.ledger.read_passages)
        expected = read_evidence_projection(session, tools.ledger)
        assert "Database is immutable" in str(expected)
        assert "next invocation resumes" in str(expected)
        assert not tools.ledger.references and store.index.count() == 0
        # Cold receipt projection; no prior model messages or network fetch.
        restored = ResearchLedger()
        restored.questions = tools.ledger.questions
        cold = TaskSession(session.policy)
        project_result_observation(cold, opened.call, opened, ok=True, research_ledger=restored)
        tools.search.text = "A later, incompatible source version."
        execute(delegate, tools, session, "open_url", {"url": URL})
        assert read_evidence_projection(cold, restored) == expected
        assert restored.read_passages == before
        assert store.index.count() == 0 and not cold._memory_results
        view = build_research_context_view(cold, restored)
        assert view.read_evidence == tuple(expected["packets"])
        assert "OR, not AND" in view.render()
    finally:
        store.close()


def test_budget_omits_entire_read_window_without_creating_unread_passages(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "## First\n" + "Long complete condition. " * 220 + "\n\n## Unread\nDo not infer this sentence."
    try:
        execute(delegate, tools, session, "open_url", {"url": URL, "limit": 5400})
        refs = dict(tools.ledger.read_passages)
        view = build_research_context_view(session, tools.ledger, budget=4000)
        assert len(view.render()) <= 4000 and not view.read_evidence
        assert view.evidence_index and view.evidence_index[0]["display_omitted"]
        assert tools.ledger.read_passages == refs
        assert all("text" not in row for row in view.evidence_index[0]["passages"])
        locator = next(row for row in view.locators if "source_id" in row)
        assert locator["next_offset"] == max(p.end for p in refs.values())
        assert locator["sections"][-1]["label"] == "Unread"
        assert "Do not infer this sentence" not in view.render()
    finally:
        store.close()


def test_search_hits_remain_locators_and_view_has_no_mutations(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "## Original\nOnly one writer can write at a time. " * 15
    try:
        execute(delegate, tools, session, "web_search", {"query": "writer"})
        execute(delegate, tools, session, "open_url", {"url": URL})
        result = execute(delegate, tools, session, "source_search", {"url": URL, "query": "writer"})
        assert result.ok
        refs = dict(tools.ledger.read_passages)
        projection = read_evidence_projection(session, tools.ledger)
        assert any("hit_id" in row and row["kind"] == "locator_not_evidence" for row in projection["locators"])
        assert any("result_id" in row for row in projection["locators"])
        assert tools.ledger.read_passages == refs and store.index.count() == 0
    finally:
        store.close()
