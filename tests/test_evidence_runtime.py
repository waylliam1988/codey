from __future__ import annotations

from codey.research.analysis_run import (
    ANALYSIS_RUN_REF_PREFIX,
    AnalysisRunRecord,
    analysis_run_record,
)
from codey.research.artifact_lineage import artifact_ref_from_managed_output
from codey.research.evidence_runtime import (
    MAX_SNAPSHOT_ANALYSIS_RUNS,
    RUNTIME_REF_KINDS,
    bounded_runtime_refs,
    is_valid_runtime_ref,
    normalize_runtime_ref,
    runtime_ref_kind,
    runtime_ref_kinds,
    snapshot_from_research_record,
)
from codey.refs import digest_json
from codey.research.object_model import (
    EvidenceLocator,
    ResearchAssumption,
    ResearchClaim,
    ResearchClaimRelation,
    ResearchEvidence,
    ResearchQuestion,
    ResearchRecord,
    ResearchSource,
)
from codey.research.proof_quality import ResearchProofReview


def _stable_hex(seed: str) -> str:
    return digest_json(seed).removeprefix("sha256:")[:16]


def _stable_sha(seed: str) -> str:
    return digest_json(seed)


def test_runtime_ref_kinds_cover_generated_and_bounded_shapes() -> None:
    kinds = runtime_ref_kinds()
    assert kinds == tuple(sorted(RUNTIME_REF_KINDS))
    for expected in (
        "source",
        "evidence",
        "claim",
        "assumption",
        "relation",
        "research_record",
        "research_proof",
        "research_plan",
        "analysis_run",
        "artifact",
        "artifact_version",
        "review_finding",
        "planner_gap",
        "run",
    ):
        assert expected in RUNTIME_REF_KINDS


def test_generated_refs_accept_exact_16_hex_suffix() -> None:
    suffix = _stable_hex("generated")
    for kind in sorted(RUNTIME_REF_KINDS - {"run"}):
        value = f"{kind}:{suffix}"
        assert is_valid_runtime_ref(value) is True, kind
        assert runtime_ref_kind(value) == kind
        assert normalize_runtime_ref(value, kind=kind) == value


def test_run_refs_accept_bounded_identifier_tokens() -> None:
    assert is_valid_runtime_ref("run:abc_123-XYZ") is True
    assert is_valid_runtime_ref("run:" + "a" * 80) is True
    assert is_valid_runtime_ref("run:" + "a" * 81) is False
    assert is_valid_runtime_ref("run:bad space") is False


def test_runtime_refs_reject_non_ref_values() -> None:
    for value in (
        "",
        None,
        "   ",
        "research_record",
        "research_record:",
        f"claim:{'A' * 16}",
        f"claim:{'g' * 16}",
        f"claim:{'a' * 15}",
        f"claim:{'a' * 17}",
        "https://example.com/claim:aaaaaaaaaaaaaaaa",
        "unknown_kind:aaaaaaaaaaaaaaaa",
        f"run:{'a' * 80}:{'b' * 60}",
    ):
        assert is_valid_runtime_ref(value) is False, value
        assert runtime_ref_kind(value) == ""


def test_kind_restriction_keeps_narrow_boundaries_narrow() -> None:
    lineage_kinds = ("source", "evidence", "analysis_run", "run")
    claim_ref = f"claim:{_stable_hex('narrow')}"
    source_ref = f"source:{_stable_hex('narrow')}"
    assert is_valid_runtime_ref(source_ref, kinds=lineage_kinds) is True
    assert is_valid_runtime_ref(claim_ref, kinds=lineage_kinds) is False
    assert is_valid_runtime_ref(claim_ref) is True
    assert normalize_runtime_ref(claim_ref, kind="evidence") == ""
    assert normalize_runtime_ref(claim_ref, kind="claim") == claim_ref


def test_bounded_runtime_refs_normalize_dedupe_and_cap() -> None:
    suffix = _stable_hex("dedupe")
    refs = [
        f"claim:{suffix} ",
        f"claim:{suffix}",
        "not-a-ref",
        "",
        f"evidence:{_stable_hex('dedupe2')}",
    ]
    assert bounded_runtime_refs(refs, limit=8) == (
        f"claim:{suffix}",
        f"evidence:{_stable_hex('dedupe2')}",
    )
    many = [f"claim:{_stable_hex(str(i))}" for i in range(10)]
    capped = bounded_runtime_refs(many, limit=3)
    assert len(capped) == 3
    assert capped == tuple(many[:3])


def _typed_record() -> ResearchRecord:
    source_id = "source:" + _stable_hex("src")
    evidence_id = "evidence:" + _stable_hex("ev")
    claim_ok = "claim:" + _stable_hex("ok")
    claim_bad = "claim:" + _stable_hex("bad")
    assumption_id = "assumption:" + _stable_hex("asm")
    relation_id = "relation:" + _stable_hex("rel")
    source = ResearchSource(
        source_id=source_id,
        final_url_ref={"url_digest": "sha256:" + _stable_hex("url"), "host": "example.com"},
        title_digest="sha256:" + _stable_hex("title"),
        content_hash=_stable_hex("content"),
        quality={"level": "primary", "kind": "official", "freshness": "fresh"},
    )
    evidence = ResearchEvidence(
        evidence_id=evidence_id,
        source_id=source_id,
        excerpt_digest="sha256:" + _stable_hex("excerpt"),
        bounded_excerpt="Opened text excerpt.",
        locator=EvidenceLocator(kind="html", source_id=source_id, char_start=0, char_end=20),
        stance="supports",
    )
    claims = (
        ResearchClaim(
            claim_id=claim_ok,
            claim_text="Backed conclusion.",
            claim_section="conclusion",
            citation_numbers=(1,),
            evidence_refs=(evidence_id,),
            status="evidence_backed",
        ),
        ResearchClaim(
            claim_id=claim_bad,
            claim_text="Unsupported conclusion.",
            claim_section="conclusion",
            status="unsupported",
        ),
    )
    relations = (
        ResearchClaimRelation(
            relation_id=relation_id,
            relation_kind="supports",
            from_ref=claim_ok,
            to_ref=evidence_id,
            citation_numbers=(1,),
        ),
    )
    question = ResearchQuestion(
        question_id="question:" + _stable_hex("q"),
        question_text_digest="sha256:" + _stable_hex("qtext"),
        chars=12,
    )
    return ResearchRecord(
        record_id="research_record:" + _stable_hex("record"),
        record_digest=_stable_sha("digest"),
        question=question,
        answer_status="partial",
        sources=(source,),
        evidence=(evidence,),
        claims=claims,
        assumptions=(
            ResearchAssumption(
                assumption_id=assumption_id,
                assumption_text="Maybe uncertain.",
                reason="unverified_assumption",
                claim_ref=claim_bad,
            ),
        ),
        relations=relations,
        unsupported_claim_count=1,
        stop_reason="done",
    )


def _proof_review(record: ResearchRecord) -> ResearchProofReview:
    return ResearchProofReview(
        ok=False,
        answers_question=False,
        answer_status="partial",
        answer_coverage_score=0.5,
        citation_present=True,
        citation_locator_verified=True,
        support_relation_verified=True,
        counterevidence_checked=True,
        ledger_record_verified=False,
        question_digest=_stable_sha("question"),
        missing_evidence=("partial_answer",),
        proof_ref="research_proof:" + _stable_hex("proof"),
        record_id=record.record_id,
        record_digest=record.record_digest,
    )


def _analysis_run() -> AnalysisRunRecord:
    record = analysis_run_record({
        "command": "python -m pytest -q",
        "tool_id": "3:1",
        "tool_name": "run",
        "ok": True,
        "exit_code": 0,
        "started_at": "2026-08-22T00:00:00Z",
        "finished_at": "2026-08-22T00:00:01Z",
        "duration_ms": 1000,
    })
    assert record is not None
    return record


def test_snapshot_projects_typed_record_into_validated_refs() -> None:
    record = _typed_record()
    review = _proof_review(record)
    run = _analysis_run()

    snapshot = snapshot_from_research_record(record, proof_review=review, analysis_runs=[run])

    assert snapshot is not None
    assert snapshot.record_ref == record.record_id
    assert snapshot.record_digest == record.record_digest
    assert snapshot.answer_status == "partial"
    assert snapshot.proof_ref.startswith("research_proof:")
    assert snapshot.question_digest == review.question_digest
    assert snapshot.source_refs == ("source:" + _stable_hex("src"),)
    assert snapshot.evidence_refs == ("evidence:" + _stable_hex("ev"),)
    assert set(snapshot.claim_refs) == {
        "claim:" + _stable_hex("ok"),
        "claim:" + _stable_hex("bad"),
    }
    assert snapshot.assumption_refs == ("assumption:" + _stable_hex("asm"),)
    assert snapshot.relation_refs == ("relation:" + _stable_hex("rel"),)
    assert snapshot.analysis_run_refs[0].startswith(ANALYSIS_RUN_REF_PREFIX)
    counts = snapshot.counts()
    assert counts["sources"] == 1
    assert counts["claims"] == 2
    assert counts["analysis_runs"] == 1


def test_snapshot_accepts_mapping_records_and_neighbors() -> None:
    typed = _typed_record()
    payload = typed.to_jsonable()
    artifact = artifact_ref_from_managed_output({
        "handle": "out-1",
        "sha256": "a" * 64,
        "stored_bytes": 128,
    })
    assert artifact is not None

    snapshot = snapshot_from_research_record(
        payload,
        proof_review={
            "proof_ref": _proof_review(typed).proof_ref,
            "question_digest": _stable_sha("question"),
        },
        analysis_runs=[_analysis_run().to_payload()],
        artifacts=[artifact.to_payload()],
    )

    assert snapshot is not None
    assert snapshot.record_ref == typed.record_id
    assert snapshot.proof_ref.startswith("research_proof:")
    assert snapshot.question_digest == _stable_sha("question")
    assert snapshot.analysis_run_refs == (_analysis_run().analysis_run_id,)
    assert snapshot.artifact_version_refs == (artifact.version_id,)
    assert snapshot.counts()["artifact_versions"] == 1


def test_snapshot_drops_unvalidatable_neighbor_entries() -> None:
    typed = _typed_record()
    snapshot = snapshot_from_research_record(
        typed,
        analysis_runs=[
            {"analysis_run_id": "analysis_run:nothex"},
            {"analysis_run_id": ""},
            "junk",
            None,
        ],
        artifacts=[{"version_id": "https://evil.example/x"}],
    )
    assert snapshot is not None
    assert snapshot.analysis_run_refs == ()
    assert snapshot.artifact_version_refs == ()


def test_snapshot_caps_neighbor_refs_like_the_trace_sections() -> None:
    typed = _typed_record()
    runs = [
        {"analysis_run_id": f"analysis_run:{_stable_hex(f'cap{i}')}"}
        for i in range(MAX_SNAPSHOT_ANALYSIS_RUNS + 4)
    ]
    snapshot = snapshot_from_research_record(typed, analysis_runs=runs)
    assert snapshot is not None
    assert len(snapshot.analysis_run_refs) == MAX_SNAPSHOT_ANALYSIS_RUNS


def test_snapshot_fails_closed_without_a_valid_record_anchor() -> None:
    assert snapshot_from_research_record(None) is None
    assert snapshot_from_research_record({"record_id": "research_record:nothex"}) is None
    assert snapshot_from_research_record({"record_id": ""}) is None
    assert snapshot_from_research_record(12345) is None


def test_snapshot_to_payload_is_stable_and_ref_only() -> None:
    typed = _typed_record()
    review = _proof_review(typed)
    first = snapshot_from_research_record(typed, proof_review=review)
    second = snapshot_from_research_record(typed, proof_review=review)
    assert first is not None and second is not None
    assert first.to_payload() == second.to_payload()
    serialized = repr(first.to_payload())
    assert "Unsupported conclusion." not in serialized
    assert "Opened text excerpt." not in serialized
