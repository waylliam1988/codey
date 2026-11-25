"""Search budgets admit distinct topic candidates and lexical terms."""

from contextlib import closing
from pathlib import Path

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.research_interest import build_research_interest_candidates
from codey.knowledge.store import KnowledgeStore


def test_duplicate_questions_do_not_fill_the_candidate_budget(tmp_path: Path) -> None:
    questions = ["Which retry limit fits?", "which retry limit fits?", "How should we measure latency?"]
    with closing(KnowledgeStore(tmp_path)) as store:
        note = KnowledgeNote("report", type="synthesis", body="Open issues", session_id="research",
                             open_questions=questions)
        store.write_note(note)

        candidates = build_research_interest_candidates(store, session_id="research", limit=2)

    assert {candidate.question for candidate in candidates} == {questions[0], questions[2]}


def test_duplicate_query_words_do_not_fill_the_lexical_budget(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        observation = KnowledgeObject("gate", "observation", "Semaphore controls worker admission", "observed_result")
        store.commit_knowledge("observation", [KnowledgeMutation(observation)])

        hits = store.search("padding " * 12 + "semaphore", depth=0)

    assert [hit.version.value.id for hit in hits] == [observation.id]
