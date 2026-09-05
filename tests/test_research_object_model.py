from __future__ import annotations

import json
import tempfile
from pathlib import Path

from codey.research import object_model
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.object_model import (
    ANSWER_STATUSES,
    CLAIM_RELATION_KINDS,
    CLAIM_STATUSES,
    EXTRACTED_RELATION_KINDS,
    RESEARCH_RECORD_KIND,
    RESEARCH_RECORD_SCHEMA_VERSION,
    ResearchClaim,
    build_research_record,
    extract_claim_candidates,
    path_ref,
    sanitize_research_url_ref,
)
from codey.research.report_quality import review_report_quality


def _report(
    url: str = "https://example.com/helium",
    *,
    conclusion: str = "Helium supply depends on gas processing.",
) -> str:
    return (
        "## 结论\n"
        f"- {conclusion} [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says helium is separated from natural gas streams.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；本轮搜索了 helium，并会被新的 primary supply data 推翻。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )


def _ledger(
    url: str = "https://example.com/helium",
    *,
    body_secret: str = "",
    stance: str = "supports",
) -> ResearchLedger:
    ledger = ResearchLedger()
    ledger.record_search("helium", [{
        "title": "Helium article",
        "url": url,
        "snippet": "Helium supply.",
    }])
    text = "Helium is separated from natural gas streams. 2026 supply note."
    if body_secret:
        text += f" {body_secret}"
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Helium article",
        text=text,
    )
    evidence = ledger.prepare_evidence_items(
        [{
            "claim": "Helium supply depends on gas processing.",
            "source_url": url,
            "excerpt": "Helium is separated from natural gas streams.",
            "stance": stance,
        }],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body="Helium is separated from natural gas streams.",
        note_type="fact",
    )
    assert not evidence.error
    ledger.add_evidence_items(list(evidence.items), note_id="fact-1")
    return ledger


def _review(summary: str, ledger: ResearchLedger):
    search_urls = {
        result.url
        for search in ledger.searches
        for result in search.results
    }
    return review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls=search_urls,
    )


def test_research_record_ids_digest_and_graph_are_stable_for_same_inputs() -> None:
    ledger = _ledger()
    summary = _report()
    review = _review(summary, ledger)
    assert review.ok

    first = build_research_record(
        question="Research helium supply",
        summary=summary,
        ledger=ledger,
        review=review,
        run_id="run-1",
        session_id="session-1",
        project="E:/codey",
        synthesis_id="synth-1",
        stop_reason="done",
    )
    second = build_research_record(
        question="Research helium supply",
        summary=summary,
        ledger=ledger,
        review=review,
        run_id="run-1",
        session_id="session-1",
        project="E:/codey",
        synthesis_id="synth-1",
        stop_reason="done",
    )

    assert first.record_id == second.record_id
    assert first.record_digest == second.record_digest
    assert [item.source_id for item in first.sources] == [item.source_id for item in second.sources]
    assert [item.evidence_id for item in first.evidence] == [
        item.evidence_id for item in second.evidence
    ]
    assert [item.claim_id for item in first.claims] == [item.claim_id for item in second.claims]
    assert first.answer_status == "answered"
    assert first.unsupported_claim_count == 0
    assert first.claims
    assert any(
        item.status == "evidence_backed"
        for item in first.claims
        if item.claim_section == "conclusion"
    )
    assert any(item.evidence_refs for item in first.claims if item.claim_section == "conclusion")
    assert first.assumptions
    assert any(item.relation_kind == "limits" for item in first.relations)

    payload = first.to_jsonable()
    assert payload["schema_version"] == RESEARCH_RECORD_SCHEMA_VERSION
    assert payload["kind"] == RESEARCH_RECORD_KIND
    assert json.dumps(payload, sort_keys=True)


def test_search_result_and_unopened_source_do_not_become_evidence() -> None:
    searched_only = ResearchLedger()
    searched_only.record_search("helium", [{
        "title": "Helium article",
        "url": "https://example.com/helium",
        "snippet": "Helium supply.",
    }])

    search_record = build_research_record(
        question="Research helium",
        summary="",
        ledger=searched_only,
        run_id="run-search-only",
    )

    assert search_record.sources == ()
    assert search_record.evidence == ()
    assert search_record.answer_status == "not_answered"

    tampered = ResearchLedger()
    tampered.evidence_items.append(EvidenceItem(
        claim="Helium supply depends on gas processing.",
        source_url="https://example.com/helium",
        excerpt="Helium is separated from natural gas streams.",
    ))

    tampered_record = build_research_record(
        question="Research helium",
        summary=_report(),
        ledger=tampered,
        run_id="run-tampered",
    )

    assert tampered_record.sources == ()
    assert tampered_record.evidence == ()


def test_same_source_evidence_does_not_support_unmatched_claim_text() -> None:
    ledger = _ledger()
    summary = _report(
        conclusion=(
            "Helium supply depends on gas processing. [1]\n"
            "- Helium prices will double next month. [1]"
        )
    )
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research helium",
        summary=summary,
        ledger=ledger,
        review=review,
        stop_reason="done",
    )

    price_claim = next(
        item for item in record.claims if "prices will double" in item.claim_text
    )
    assert price_claim.status == "unsupported"
    assert price_claim.citation_numbers == (1,)
    assert price_claim.evidence_refs == ()
    assert not any(
        relation.from_ref == price_claim.claim_id and relation.relation_kind == "supports"
        for relation in record.relations
    )
    assert record.unsupported_claim_count == 1
    assert record.answer_status == "partial"


def test_same_citation_paraphrased_claim_binds_to_evidence_claim_text() -> None:
    url = "https://pubmed.ncbi.nlm.nih.gov/42577735/"
    ledger = ResearchLedger()
    ledger.record_search("ICI hepatitis MMF", [{
        "title": "ICI hepatitis study",
        "url": url,
        "snippet": "Steroid refractory ICI hepatitis.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="ICI hepatitis study",
        text="Of 10 steroid-refractory immune-mediated hepatitis cases, 9 improved after treatment.",
    )
    evidence = ledger.prepare_evidence_items(
        [{
            "claim": "MMF improved steroid refractory ICI hepatitis in treated patients.",
            "source_url": url,
            "excerpt": "Of 10 steroid-refractory immune-mediated hepatitis cases, 9 improved after treatment.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="MMF improved steroid refractory ICI hepatitis in treated patients.",
        fallback_body="Of 10 steroid-refractory immune-mediated hepatitis cases, 9 improved after treatment.",
        note_type="fact",
    )
    assert not evidence.error
    ledger.add_evidence_items(list(evidence.items), note_id="fact-ici")
    summary = (
        "## 结论\n"
        "- MMF improved steroid-refractory ICI hepatitis cases. [1]\n\n"
        "## 关键证据\n"
        "- MMF improved steroid-refractory ICI hepatitis cases. [1]\n\n"
        "## 反证与限制\n"
        "- 未找到强反证。\n\n"
        "## 来源质量\n"
        "- [1] PubMed abstract.\n\n"
        "## 搜索覆盖\n"
        "- query: ICI hepatitis MMF\n\n"
        "## 来源\n"
        f"[1] ICI hepatitis study - {url}"
    )
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research ICI hepatitis",
        summary=summary,
        ledger=ledger,
        review=review,
        stop_reason="done",
    )

    conclusion_claim = next(
        item for item in record.claims if item.claim_section == "conclusion"
    )
    assert conclusion_claim.status == "evidence_backed"
    assert conclusion_claim.evidence_refs == (record.evidence[0].evidence_id,)
    assert "claim_text" not in record.evidence[0].to_jsonable()
    assert any(
        relation.from_ref == conclusion_claim.claim_id
        and relation.to_ref == record.evidence[0].evidence_id
        and relation.relation_kind == "supports"
        for relation in record.relations
    )


def test_same_citation_overlap_requires_specific_content_terms() -> None:
    url = "https://pubmed.ncbi.nlm.nih.gov/42577735/"
    ledger = ResearchLedger()
    ledger.record_search("ICI hepatitis MMF", [{
        "title": "ICI hepatitis study",
        "url": url,
        "snippet": "Steroid refractory ICI hepatitis.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="ICI hepatitis study",
        text="Of 10 steroid-refractory immune-mediated hepatitis cases, 9 improved after treatment.",
    )
    evidence = ledger.prepare_evidence_items(
        [{
            "claim": "MMF improved steroid refractory ICI hepatitis in treated patients.",
            "source_url": url,
            "excerpt": "Of 10 steroid-refractory immune-mediated hepatitis cases, 9 improved after treatment.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="MMF improved steroid refractory ICI hepatitis in treated patients.",
        fallback_body="Of 10 steroid-refractory immune-mediated hepatitis cases, 9 improved after treatment.",
        note_type="fact",
    )
    assert not evidence.error
    ledger.add_evidence_items(list(evidence.items), note_id="fact-ici")
    summary = (
        "## 结论\n"
        "- Tacrolimus prevents ICI hepatitis recurrence. [1]\n\n"
        "## 关键证据\n"
        "- Tacrolimus prevents ICI hepatitis recurrence. [1]\n\n"
        "## 反证与限制\n"
        "- 未找到强反证。\n\n"
        "## 来源质量\n"
        "- [1] PubMed abstract.\n\n"
        "## 搜索覆盖\n"
        "- query: ICI hepatitis MMF\n\n"
        "## 来源\n"
        f"[1] ICI hepatitis study - {url}"
    )
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research ICI hepatitis",
        summary=summary,
        ledger=ledger,
        review=review,
        stop_reason="done",
    )

    conclusion_claim = next(
        item for item in record.claims if item.claim_section == "conclusion"
    )
    assert conclusion_claim.status == "unsupported"
    assert conclusion_claim.evidence_refs == ()
    assert not any(
        relation.from_ref == conclusion_claim.claim_id and relation.relation_kind == "supports"
        for relation in record.relations
    )


def test_same_citation_numeric_claim_binds_when_distinct_values_match_evidence() -> None:
    url = "https://pubmed.ncbi.nlm.nih.gov/41972337/"
    ledger = ResearchLedger()
    ledger.record_search("ICI hepatotoxicity multicenter", [{
        "title": "TOG study",
        "url": url,
        "snippet": "Grade 3-4 liver dysfunction in ICI hepatotoxicity.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="TOG study",
        text=(
            "In the cohort, 24/56 patients (42.9%) developed grade 3-4 liver "
            "dysfunction, and 5/56 patients (8.9%) died due to liver failure."
        ),
    )
    evidence = ledger.prepare_evidence_items(
        [{
            "claim": "24/56 patients (42.9%) developed grade 3-4 liver dysfunction.",
            "source_url": url,
            "excerpt": (
                "In the cohort, 24/56 patients (42.9%) developed grade 3-4 liver "
                "dysfunction, and 5/56 patients (8.9%) died due to liver failure."
            ),
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="24/56 patients (42.9%) developed grade 3-4 liver dysfunction.",
        fallback_body="24/56 patients (42.9%) developed grade 3-4 liver dysfunction.",
        note_type="fact",
    )
    assert not evidence.error
    ledger.add_evidence_items(list(evidence.items), note_id="fact-tog")
    summary = (
        "## 结论\n"
        "- 42.9%（24/56）的患者出现3-4级肝功能不全。 [1]\n\n"
        "## 关键证据\n"
        "- 42.9%（24/56）的患者出现3-4级肝功能不全。 [1]\n\n"
        "## 反证与限制\n"
        "- 未找到强反证。\n\n"
        "## 来源质量\n"
        "- [1] PubMed multicenter study.\n\n"
        "## 搜索覆盖\n"
        "- query: ICI hepatotoxicity multicenter\n\n"
        "## 来源\n"
        f"[1] TOG study - {url}"
    )
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research ICI hepatotoxicity",
        summary=summary,
        ledger=ledger,
        review=review,
        stop_reason="done",
    )

    conclusion_claim = next(
        item for item in record.claims if item.claim_section == "conclusion"
    )
    assert conclusion_claim.status == "evidence_backed"
    assert conclusion_claim.evidence_refs == (record.evidence[0].evidence_id,)
    assert any(
        relation.from_ref == conclusion_claim.claim_id
        and relation.to_ref == record.evidence[0].evidence_id
        and relation.relation_kind == "supports"
        for relation in record.relations
    )


def test_contradicting_evidence_does_not_support_conclusion_claim() -> None:
    ledger = _ledger(stance="contradicting")
    summary = _report()
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research helium",
        summary=summary,
        ledger=ledger,
        review=review,
        stop_reason="done",
    )

    conclusion_claim = next(
        item for item in record.claims if item.claim_section == "conclusion"
    )
    assert record.evidence[0].stance == "contradicts"
    assert conclusion_claim.status == "unsupported"
    assert conclusion_claim.evidence_refs == ()
    assert not any(
        relation.from_ref == conclusion_claim.claim_id and relation.relation_kind == "supports"
        for relation in record.relations
    )


def test_unknown_nonempty_evidence_stance_does_not_support_conclusion_claim() -> None:
    ledger = _ledger(stance="maybe_supports")
    summary = _report()
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research helium",
        summary=summary,
        ledger=ledger,
        review=review,
        stop_reason="done",
    )

    conclusion_claim = next(
        item for item in record.claims if item.claim_section == "conclusion"
    )
    assert record.evidence[0].stance == "unknown"
    assert conclusion_claim.status == "unsupported"
    assert conclusion_claim.evidence_refs == ()
    assert not any(
        relation.from_ref == conclusion_claim.claim_id and relation.relation_kind == "supports"
        for relation in record.relations
    )


def test_counter_section_accepts_refutes_and_context_stances() -> None:
    summary = (
        "## 结论\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 关键证据\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 反证与限制\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        "[1] Helium article - https://example.com/helium"
    )
    cases = (
        ("refutes", "contradicts", "refutes"),
        ("refutation", "contradicts", "refutes"),
        ("opposes", "contradicts", "refutes"),
        ("context", "context", "limits"),
        ("limitations", "context", "limits"),
    )

    for raw_stance, expected_stance, expected_relation in cases:
        ledger = _ledger(stance=raw_stance)
        review = _review(summary, ledger)
        assert review.ok

        record = build_research_record(
            question="Research helium",
            summary=summary,
            ledger=ledger,
            review=review,
            stop_reason="done",
        )

        counter_claim = next(
            item for item in record.claims if item.claim_section == "counter"
        )
        assert record.evidence[0].stance == expected_stance
        assert counter_claim.status == "evidence_backed"
        assert counter_claim.evidence_refs == (record.evidence[0].evidence_id,)
        assert any(
            relation.from_ref == counter_claim.claim_id
            and relation.to_ref == record.evidence[0].evidence_id
            and relation.relation_kind == expected_relation
            for relation in record.relations
        )
        assert not any(
            relation.from_ref == counter_claim.claim_id
            and relation.relation_kind == "supports"
            for relation in record.relations
        )


def test_claim_graph_prunes_assumption_refs_after_assumption_cap() -> None:
    counter_lines = "\n".join(
        f"- 可能需要进一步验证 scenario {index}"
        for index in range(20)
    )

    claims, assumptions, relations, unsupported = extract_claim_candidates(
        sections={"counter": counter_lines},
        citation_urls={},
        evidence_by_source={},
        source_ids_by_url={},
    )

    assumption_ids = {item.assumption_id for item in assumptions}
    claim_ids = {item.claim_id for item in claims}

    assert len(claims) == 20
    assert len(assumptions) == 16
    assert unsupported == 4
    assert all(
        ref in assumption_ids
        for claim in claims
        for ref in claim.assumption_refs
    )
    assert all(relation.from_ref in claim_ids for relation in relations)
    assert all(relation.to_ref in assumption_ids for relation in relations)


def test_only_counter_assumptions_create_limits_relations() -> None:
    claims, assumptions, relations, unsupported = extract_claim_candidates(
        sections={
            "conclusion": "- likely helium routing changed",
            "evidence": "- 可能 evidence gap remains",
            "counter": "- 可能需要进一步验证 supply data",
        },
        citation_urls={},
        evidence_by_source={},
        source_ids_by_url={},
    )

    counter_claim = next(item for item in claims if item.claim_section == "counter")
    non_counter_claim_ids = {
        item.claim_id for item in claims if item.claim_section in {"conclusion", "evidence"}
    }

    assert len(claims) == 3
    assert len(assumptions) == 3
    assert unsupported == 0
    assert len(relations) == 1
    assert relations[0].relation_kind == "limits"
    assert relations[0].from_ref == counter_claim.claim_id
    assert not any(relation.from_ref in non_counter_claim_ids for relation in relations)


def test_url_refs_are_redacted_and_source_bodies_are_not_persisted() -> None:
    url = "https://user:password@example.com/secure/path?token=SECRET_TOKEN&ok=public"
    ledger = _ledger(url, body_secret="PRIVATE_SOURCE_BODY_SENTINEL")
    summary = _report(url)
    review = _review(summary, ledger)
    assert review.ok

    record = build_research_record(
        question="Research a credentialed URL",
        summary=summary,
        ledger=ledger,
        review=review,
        run_id="run-secret",
    )
    serialized = json.dumps(record.to_jsonable(), ensure_ascii=False)

    assert "user:password" not in serialized
    assert "SECRET_TOKEN" not in serialized
    assert url not in serialized
    assert "PRIVATE_SOURCE_BODY_SENTINEL" not in serialized
    assert "Helium is separated from natural gas streams." in serialized
    assert record.sources[0].final_url_ref["redacted"] is True
    assert record.sources[0].host == "example.com"


def test_enums_and_v1_generated_relation_kinds_are_bounded() -> None:
    assert ANSWER_STATUSES == frozenset({
        "answered",
        "partial",
        "insufficient_evidence",
        "not_answered",
    })
    assert CLAIM_STATUSES == frozenset({
        "evidence_backed",
        "unsupported",
        "assumption",
    })
    assert CLAIM_RELATION_KINDS == frozenset({
        "supports",
        "refutes",
        "updates",
        "supersedes",
        "conflicts_with",
        "limits",
    })
    ledger = _ledger()
    summary = _report()
    record = build_research_record(
        question="Research helium",
        summary=summary,
        ledger=ledger,
        review=_review(summary, ledger),
        stop_reason="done",
    )

    generated = {item.relation_kind for item in record.relations}
    assert generated
    assert generated.issubset(EXTRACTED_RELATION_KINDS)
    assert "updates" not in generated
    assert "supersedes" not in generated
    assert "conflicts_with" not in generated


def test_claim_status_serialization_does_not_keep_old_supported_value() -> None:
    claim = ResearchClaim(
        claim_id="claim:legacy",
        claim_text="Legacy status should not survive.",
        claim_section="conclusion",
        status="supported",
    )

    assert claim.to_jsonable()["status"] == "unsupported"


def test_path_and_url_helpers_do_not_expose_local_absolute_paths_or_secrets() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        local_file = project / "data" / "secret.csv"
        local_file.parent.mkdir()
        local_file.write_text("secret", encoding="utf-8")

        local_ref = path_ref(local_file, project=project)
        url_ref = sanitize_research_url_ref(
            "https://api.example.com/items?api_key=SECRET_KEY&query=helium"
        )
        serialized = json.dumps({"path": local_ref, "url": url_ref}, ensure_ascii=False)

        assert str(project) not in serialized
        assert str(local_file) not in serialized
        assert "SECRET_KEY" not in serialized
        assert local_ref["basename"] == "secret.csv"
        assert url_ref["host"] == "api.example.com"
        assert url_ref["redacted"] is True


def test_url_redaction_covers_secret_key_variants_without_digesting_values() -> None:
    first = sanitize_research_url_ref(
        "https://api.example.com/items?"
        "client_secret=FIRST&refresh_token=FIRST&x-api-key=FIRST&jwt=FIRST&"
        "session_id=FIRST&authorization=FIRST&bearer=FIRST&credentials=FIRST&ok=public"
    )
    second = sanitize_research_url_ref(
        "https://api.example.com/items?"
        "client_secret=SECOND&refresh_token=SECOND&x-api-key=SECOND&jwt=SECOND&"
        "session_id=SECOND&authorization=SECOND&bearer=SECOND&credentials=SECOND&ok=public"
    )
    serialized = json.dumps({"first": first, "second": second}, ensure_ascii=False)

    assert first["redacted"] is True
    assert second["redacted"] is True
    assert first["url_digest"] == second["url_digest"]
    assert "FIRST" not in serialized
    assert "SECOND" not in serialized

    protocol_relative = sanitize_research_url_ref(
        "//api.example.com/items?client_secret=THIRD&ok=public"
    )
    assert protocol_relative["redacted"] is True

    no_host_first = sanitize_research_url_ref("/items?sessionid=FIRST&ok=public")
    no_host_second = sanitize_research_url_ref("/items?sessionid=SECOND&ok=public")
    malformed_first = sanitize_research_url_ref("https://[bad?api_key=FIRST&ok=public")
    malformed_second = sanitize_research_url_ref("https://[bad?api_key=SECOND&ok=public")
    malformed_userinfo_first = sanitize_research_url_ref("https://user:PASS_A@[bad")
    malformed_userinfo_second = sanitize_research_url_ref("https://user:PASS_B@[bad")
    query_key_first = sanitize_research_url_ref("https://api.example.com/items?FIRST_SECRET=1")
    query_key_second = sanitize_research_url_ref("https://api.example.com/items?SECOND_SECRET=1")

    assert no_host_first["redacted"] is True
    assert malformed_first["redacted"] is True
    assert malformed_userinfo_first["redacted"] is True
    assert query_key_first["redacted"] is True
    assert no_host_first["url_digest"] == no_host_second["url_digest"]
    assert malformed_first["url_digest"] == malformed_second["url_digest"]
    assert malformed_userinfo_first["url_digest"] == malformed_userinfo_second["url_digest"]
    assert query_key_first["url_digest"] == query_key_second["url_digest"]

def test_long_excerpt_keeps_exact_text_and_valid_char_locator() -> None:
    """An excerpt longer than the 360-char display cap must not corrupt the
    proof locator: char offsets are computed from the exact matched text and
    the stored excerpt stays verbatim (no "..." marker) until presentation.
    """
    url = "https://example.com/long-helium"
    ledger = ResearchLedger()
    filler = ("Helium markets stayed tight through the quarter, with traders "
              "watching every cargo and inventory report for direction. ") * 6
    long_excerpt = (
        "Helium is separated from natural gas streams in ways that shape the "
        + filler
        + "final 2026 supply outlook for industrial buyers."
    )
    source_text = (
        "Intro paragraph that is not part of any evidence item. "
        + long_excerpt
        + " Trailing analysis follows the quoted region."
    )
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Long helium article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Supply stays tight.",
            "source_url": url,
            "excerpt": long_excerpt,
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Supply stays tight.",
        fallback_body=long_excerpt[:200],
        note_type="fact",
    )

    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="fact-long")
    stored = prepared.items[0]
    assert len(stored.excerpt) > 360          # longer than the display cap...
    assert not stored.excerpt.endswith("...") # ...yet still the exact match

    # Public payload boundary (events / UI session state) carries only the
    # bounded display form; the internal item stays exact for locators.
    payload_excerpt = ledger.evidence_payload()[0]["excerpt"]
    assert payload_excerpt != stored.excerpt
    assert payload_excerpt.endswith("...")
    assert len(payload_excerpt) <= 363

    start, end = object_model._excerpt_offsets(source_text, stored.excerpt)
    assert start > 0 and end > start
    record = build_research_record(
        question="Research helium supply",
        summary="## 结论\n- Tight [1]\n\n## 来源\n[1] Long - " + url,
        ledger=ledger,
        review=None,
        run_id="run-long",
        session_id="session-long",
        stop_reason="done",
    )
    evidence_row = record.evidence[0]
    locator = evidence_row.locator.to_jsonable()
    assert locator["char_start"] == start
    assert locator["char_end"] == end
