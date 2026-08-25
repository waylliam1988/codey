from __future__ import annotations

import json

from codey.research.topic_continuity import (
    CONTEXT_SOURCE_KEY,
    ITEM_KIND_CORRECTION,
    ITEM_KIND_OPEN_QUESTION,
    ITEM_KIND_PREFERENCE,
    ITEM_KIND_PRIOR_CLAIM,
    MAX_TOPIC_CANDIDATES,
    MAX_TOPIC_CLAIM_REFS,
    PROMPT_SOURCE_REF,
    TopicContinuityItem,
    TopicPlannerCandidate,
    build_topic_candidates,
    project_topic_continuity,
    render_topic_continuity,
    topic_item,
)

# Item-text screening follows the codebase-wide internal-vocabulary filter.
# "memory" is deliberately not an item-text ban (it is common research
# content, e.g. memory paging); it is enforced on framing lines instead.
_BANNED_ITEM_TERMS = ("ghost", "work queue", "concept graph")
_BANNED_FRAMING_TERMS = ("ghost", "memory", "work queue", "concept graph")


def _interest_hint(question: str, *, ref: str = "research_interest:ric_1") -> dict[str, object]:
    return {
        "ref": ref,
        "question": question,
        "why_now": "Open question from a bounded local Research note.",
        "priority": 0.7,
        "confidence": 0.8,
        "strong_support": True,
        "source": "research_note",
        "source_ref": "note-1",
    }


def test_empty_inputs_project_to_empty_baseline() -> None:
    projection = project_topic_continuity()

    assert not projection.admitted
    assert projection.prompt_text == ""
    assert projection.items == ()
    assert projection.candidates == ()
    payload = projection.to_payload()
    assert payload["admitted"] is False
    assert payload["item_count"] == 0
    assert payload["items"] == []


def test_interest_hint_becomes_open_question_item_and_candidate() -> None:
    projection = project_topic_continuity(
        interest_hints=(_interest_hint("Does the 2025 finding still hold?"),),
    )

    assert projection.admitted
    assert len(projection.items) == 1
    item = projection.items[0]
    assert item.kind == ITEM_KIND_OPEN_QUESTION
    assert item.refs == ("research_interest:ric_1",)
    assert "Does the 2025 finding still hold?" in projection.prompt_text
    assert "not evidence" in projection.prompt_text
    assert "Do not cite this section" in projection.prompt_text
    assert len(projection.candidates) == 1


def test_ghost_continuity_hints_map_to_bounded_kinds() -> None:
    projection = project_topic_continuity(
        continuity_hints=(
            {"id": "cont_oq", "kind": "open_question", "text": "Is X still true?", "weight": 0.3},
            {
                "id": "cont_corr",
                "kind": "fresh_correction",
                "text": "Earlier answer corrected",
                "weight": 0.4,
            },
            {
                "id": "cont_pref",
                "kind": "recently_reinforced_preference",
                "text": "Prefer concise summaries",
                "weight": 0.2,
            },
            # Chat-side kinds must not leak into research prompts.
            {"id": "cont_focus", "kind": "recent_focus", "text": "Recent focus", "weight": 0.9},
            {"id": "cont_goal", "kind": "long_term_goal", "text": "Long goal", "weight": 0.9},
        ),
    )

    kinds = sorted(item.kind for item in projection.items)
    assert kinds == ["correction", "open_question", "preference"]
    corrections = [item for item in projection.items if item.kind == ITEM_KIND_CORRECTION]
    assert corrections[0].stale is True
    assert "needs_recheck" in corrections[0].reason_codes[0]


def test_prior_claim_refs_are_always_stale_and_never_evidence() -> None:
    projection = project_topic_continuity(
        claim_refs=({"ref": "prior_claim:abc"}, "def"),
    )

    claims = [item for item in projection.items if item.kind == ITEM_KIND_PRIOR_CLAIM]
    assert [item.refs[0] for item in claims] == ["prior_claim:abc", "prior_claim:def"]
    assert all(item.stale for item in claims)
    assert all("prior_claim_needs_recheck" in item.reason_codes for item in claims)
    payload = json.dumps(projection.to_payload())
    assert "evidence_refs" not in payload
    assert all(
        row["stale"] is True
        for row in projection.to_payload()["items"]
        if row["kind"] == ITEM_KIND_PRIOR_CLAIM
    )


def test_projection_payload_is_digest_only() -> None:
    question = "Does the sensitive 2026 copper supply claim still hold?"
    projection = project_topic_continuity(
        interest_hints=(_interest_hint(question),),
        claim_refs=("claim-1",),
    )
    raw = json.dumps(projection.to_payload(), ensure_ascii=False)

    assert question not in raw
    assert "why_now" not in raw
    assert raw.count("sha256:") == 1  # exactly one content digest; no text bodies
    assert projection.to_payload()["context_source"] == CONTEXT_SOURCE_KEY
    assert projection.to_payload()["digest"].startswith("sha256:")


def test_merge_free_duplicate_questions_unify_refs_and_flag_risk() -> None:
    projection = project_topic_continuity(
        interest_hints=(_interest_hint("Is helium supply still constrained?"),),
        continuity_hints=(
            {"id": "cont_oq", "kind": "open_question", "text": "Is helium supply still constrained?"},
        ),
    )

    open_items = [
        item for item in projection.items if item.kind == ITEM_KIND_OPEN_QUESTION
    ]
    assert len(open_items) == 1
    assert set(open_items[0].to_payload().keys()) == {"refs", "kind", "stale", "reason_codes"}
    assert len(projection.candidates) == 1
    assert set(projection.candidates[0].source_refs) == {
        "research_interest:ric_1",
        "continuity:cont_oq",
    }
    assert projection.candidates[0].risk_codes == ("repeated_open_question",)


def test_internal_vocabulary_and_secrets_are_dropped() -> None:
    projection = project_topic_continuity(
        interest_hints=(
            _interest_hint("Research whether the Ghost store needs a Work Queue.", ref="r1"),
            _interest_hint("What is the api_key rotation policy?", ref="r2"),
            # Common content words stay admissible: "memory" here is a
            # research topic, not internal machinery vocabulary.
            _interest_hint("How does memory paging behave under load?", ref="r3"),
        ),
    )

    assert projection.admitted
    # Warnings are deduplicated by design; two drops collapse into one code.
    assert "interest_hint_skipped" in projection.warnings
    assert all(
        not any(term in item.text.casefold() for term in _BANNED_ITEM_TERMS)
        for item in projection.items
    )


def test_framing_vocabulary_never_contains_internal_names() -> None:
    # The framing/header lines are Codey-authored, so they must avoid every
    # roadmap-internal name including "memory"; only seeded item text may
    # legitimately carry common words.
    projection = project_topic_continuity(
        interest_hints=(_interest_hint("Does the 2026 supply claim still hold?"),),
        continuity_hints=(
            {"id": "p", "kind": "recently_reinforced_preference", "text": "Be concise"},
        ),
        claim_refs=("old",),
    )
    lowered = projection.prompt_text.casefold()

    assert not any(term in lowered for term in _BANNED_FRAMING_TERMS)


def test_candidate_ranking_is_bounded_and_deterministic() -> None:
    hints = tuple(
        _interest_hint(f"Question number {index} about topic {index}?", ref=f"r{index}")
        for index in range(MAX_TOPIC_CANDIDATES + 4)
    )
    first = project_topic_continuity(interest_hints=hints)
    second = project_topic_continuity(interest_hints=hints)

    assert len(first.candidates) == MAX_TOPIC_CANDIDATES
    assert first.candidates == second.candidates
    assert first.truncated is True


def test_budget_truncation_keeps_only_what_fits() -> None:
    header = (
        "Local research continuity. This is not evidence.\n"
        "Treat every line below as a lead that may need re-checking; verify "
        "against opened sources before factual claims. Do not cite this section."
    )
    long_question = "A very long question " * 40

    # A budget too small for even the framing header admits nothing.
    assert render_topic_continuity(
        candidates=(TopicPlannerCandidate("topic_x", long_question),),
        budget_chars=10,
    ).text == ""
    # Lines that cannot fit are skipped; smaller leads still get packed
    # greedily in priority order.
    partial = render_topic_continuity(
        candidates=(
            TopicPlannerCandidate("topic_a", "Short lead?"),
            TopicPlannerCandidate("topic_b", long_question),
        ),
        budget_chars=len(header) + 200,
    )

    assert "Short lead?" in partial.text
    assert partial.emitted_lines == 1
    assert partial.skipped_lines == 1
    assert "very long question" not in partial.text

    # A budget too small for even the framing header admits nothing.
    assert render_topic_continuity(
        candidates=(TopicPlannerCandidate("topic_x", "q"),),
        budget_chars=10,
    ).text == ""


def test_budget_skips_are_reported_as_truncated_by_the_projection() -> None:
    header = (
        "Local research continuity. This is not evidence.\n"
        "Treat every line below as a lead that may need re-checking; verify "
        "against opened sources before factual claims. Do not cite this section."
    )
    projection = project_topic_continuity(
        interest_hints=(
            _interest_hint(f"Lead number {index} about supply?", ref=f"r{index}")
            for index in range(3)
        ),
        budget_chars=len(header) + 120,  # fits only some lead lines
    )

    assert projection.admitted
    assert projection.truncated is True  # render drops are visible in trace
    payload = projection.to_payload()
    assert payload["truncated"] is True


def test_zero_budget_admits_nothing() -> None:
    rendering = render_topic_continuity(
        candidates=(TopicPlannerCandidate("t", "q"),),
        budget_chars=0,
    )
    assert rendering.text == ""
    assert rendering.emitted_lines == 0
    assert rendering.skipped_lines == 0


def test_claim_ref_cap_reports_truncation_honestly() -> None:
    projection = project_topic_continuity(
        claim_refs=tuple(f"claim-{index}" for index in range(MAX_TOPIC_CLAIM_REFS + 2)),
    )

    claims = [item for item in projection.items if item.kind == ITEM_KIND_PRIOR_CLAIM]
    assert len(claims) == MAX_TOPIC_CLAIM_REFS
    assert projection.claim_ref_count == MAX_TOPIC_CLAIM_REFS
    assert projection.truncated is True
    payload = projection.to_payload()
    assert payload["claim_ref_count"] == MAX_TOPIC_CLAIM_REFS
    assert payload["truncated"] is True


def test_topic_item_factory_fails_closed_on_unusable_input() -> None:
    assert topic_item(kind="", ref="r", text="t") is None
    assert topic_item(kind="open_question", ref="", text="t") is None
    assert topic_item(kind="open_question", ref="r", text="") is None
    assert topic_item(kind="open_question", ref="r\nt", text="t") is None


def test_projection_invariant_refs_only_relocate_history() -> None:
    """Continuity may relocate old refs; it can never mint evidence rows."""
    projection = project_topic_continuity(
        interest_hints=(_interest_hint("Re-check the 2025 war-helium link."),),
        continuity_hints=(
            {"id": "c1", "kind": "fresh_correction", "text": "Corrected earlier"},
        ),
        claim_refs=("old-claim",),
    )

    for item in projection.items:
        payload = item.to_payload()
        assert set(payload) == {"refs", "kind", "stale", "reason_codes"}
    prior = [
        item for item in projection.items
        if any(ref.startswith("prior_claim:") for ref in item.refs)
    ]
    assert prior and all(item.stale for item in prior)
    lowered = projection.prompt_text.casefold()
    assert not any(term in lowered for term in _BANNED_FRAMING_TERMS)
    assert PROMPT_SOURCE_REF == "local_context:research_topic_continuity"


def test_standalone_items_render_through_shared_path() -> None:
    correction = TopicContinuityItem(
        kind=ITEM_KIND_CORRECTION,
        text="Fixed stale figure",
        refs=("continuity:c1",),
        stale=True,
        reason_codes=("needs_recheck",),
    )
    candidates = build_topic_candidates((
        TopicContinuityItem(kind=ITEM_KIND_OPEN_QUESTION, text="Still true?", refs=("note:1",)),
        correction,
    ))

    assert len(candidates) == 1
    rendering = render_topic_continuity(
        candidates=candidates,
        corrections=(correction,),
        preferences=(
            TopicContinuityItem(
                kind=ITEM_KIND_PREFERENCE,
                text="Short answers",
                refs=("continuity:p1",),
            ),
        ),
        claim_ref_count=3,
    )

    assert "Suggested next-research topics" in rendering.text
    assert "Still true?" in rendering.text
    assert "Fixed stale figure" in rendering.text
    assert "Short answers" in rendering.text
    assert "3 prior claim(s)" in rendering.text
