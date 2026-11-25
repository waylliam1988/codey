"""The handoff checks citations before clipping the displayed conclusion."""

from contextlib import closing
from pathlib import Path

from codey.knowledge.brief import BRIEF_TOTAL_LIMIT, KnowledgeBriefBuilder
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore


def test_clipping_cannot_hide_an_unknown_citation_from_validation(tmp_path: Path) -> None:
    detail = "More context about the decision. " * 10
    with closing(KnowledgeStore(tmp_path)) as store:
        note = KnowledgeNote(
            "research", type="synthesis", session_id="session",
            body=(
                "## Conclusion\n"
                f"- Supported decision [1]. {detail}\n"
                f"- Mixed evidence [1]. {detail}[99]\n\n"
                "## Sources\n"
                "[1] Known source - https://example.com/one\n"
            ),
        )
        store.write_note(note)
        brief = KnowledgeBriefBuilder(store).build_for_session("session")

    assert len(brief.conclusions) == 1
    assert brief.conclusions[0].startswith("Supported decision [1].")
    assert len(brief.counterpoints) == 1
    assert brief.counterpoints[0].startswith("Mixed evidence [1].")
    assert brief.counterpoints[0].endswith("[uncited]")
    assert len(brief.render()) <= BRIEF_TOTAL_LIMIT
