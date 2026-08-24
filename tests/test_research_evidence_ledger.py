from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from codey.research.evidence_ledger import (
    EVIDENCE_LEDGER_KIND,
    EVIDENCE_LEDGER_SCHEMA_VERSION,
    EvidenceLedgerStore,
    MAX_EVIDENCE_LEDGER_BYTES,
    MAX_LEDGER_EVIDENCE,
)
from codey.research.ledger import ResearchLedger
from codey.research.object_model import (
    EvidenceLocator,
    ResearchAssumption,
    ResearchClaim,
    ResearchClaimRelation,
    ResearchEvidence,
    ResearchQuestion,
    ResearchRecord,
    ResearchSource,
    build_research_record,
)
from codey.research.report_quality import review_report_quality


def _report(url: str) -> str:
    return (
        "## 结论\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says helium is separated from natural gas streams.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新供应数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )


def _record(
    *,
    url: str = "https://user:password@example.com/secure/path?token=SECRET_TOKEN",
    project: str | Path | None = None,
    run_id: str = "run-ledger",
    session_id: str = "session-ledger",
) -> ResearchRecord:
    ledger = ResearchLedger()
    source_text = (
        "Helium is separated from natural gas streams. 2026 supply note. "
        "PRIVATE_SOURCE_BODY_SENTINEL"
    )
    ledger.record_search("helium", [{
        "title": "Helium article",
        "url": url,
        "snippet": "Search result is not evidence.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Secret Source Title",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Helium supply depends on gas processing.",
            "source_url": url,
            "excerpt": "Helium is separated from natural gas streams.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body=source_text,
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-secret")
    summary = _report(url)
    review = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert review.ok
    return build_research_record(
        question="Research helium supply with SECRET_QUESTION_TOKEN",
        summary=summary,
        ledger=ledger,
        review=review,
        run_id=run_id,
        session_id=session_id,
        project=project,
        synthesis_id="synth-1",
        stop_reason="done",
    )


def _wide_record(index: int, *, evidence_count: int = 32) -> ResearchRecord:
    source_id = f"source:{index:016x}"
    evidence: list[ResearchEvidence] = []
    claims: list[ResearchClaim] = []
    relations: list[ResearchClaimRelation] = []
    for item in range(evidence_count):
        evidence_id = f"evidence:{index:04x}_{item:04x}"
        claim_id = f"claim:{index:04x}_{item:04x}"
        evidence.append(ResearchEvidence(
            evidence_id=evidence_id,
            source_id=source_id,
            excerpt_digest="sha256:" + f"{index:032x}{item:032x}"[-64:],
            bounded_excerpt=f"bounded excerpt {index}-{item}",
            locator=EvidenceLocator(
                kind="html",
                source_id=source_id,
                char_start=item * 10,
                char_end=item * 10 + 8,
            ),
            stance="supports",
            claim_text_digest="sha256:" + f"{item:064x}",
        ))
        claims.append(ResearchClaim(
            claim_id=claim_id,
            claim_text=f"Claim {index}-{item}",
            claim_section="conclusion",
            evidence_refs=(evidence_id,),
            status="evidence_backed",
        ))
        relations.append(ResearchClaimRelation(
            relation_id=f"relation:{index:04x}_{item:04x}",
            relation_kind="supports",
            from_ref=claim_id,
            to_ref=evidence_id,
        ))
    return ResearchRecord(
        record_id=f"research_record:{index:016x}",
        record_digest="sha256:" + f"{index:064x}",
        question=ResearchQuestion(
            question_id=f"question:{index:016x}",
            question_text_digest="sha256:" + f"{index + 1:064x}",
            chars=20,
        ),
        answer_status="answered",
        sources=(ResearchSource(
            source_id=source_id,
            final_url_ref={
                "url_digest": "sha256:" + f"{index + 2:064x}",
                "host": "example.com",
            },
            title_digest="sha256:" + f"{index + 3:064x}",
            content_hash=f"{index:016x}",
            content_kind="html",
        ),),
        evidence=tuple(evidence),
        claims=tuple(claims),
        relations=tuple(relations),
        run_id=f"run-{index}",
        session_id="session-ledger",
        stop_reason="done",
    )


def _capsule_record(
    index: int,
    *,
    source_id: str | None = None,
    source_host: str = "example.com",
    source_requested_url_ref: dict[str, object] | None = None,
    source_final_url_ref: dict[str, object] | None = None,
    source_retrieved_at: str = "",
    source_page_count: int = 0,
    source_pages_read: tuple[int, ...] = (),
    source_truncated: bool = False,
    source_quality: dict[str, object] | None = None,
    evidence_id: str | None = None,
    evidence_excerpt: str = "bounded excerpt",
    claim_id: str | None = None,
    claim_text: str = "Evidence-backed claim",
    assumption_id: str | None = None,
    assumption_text: str = "Declared assumption",
    relation_id: str | None = None,
    relation_kind: str = "supports",
) -> ResearchRecord:
    source_ref = source_id or f"source:{index:016x}"
    evidence_ref = evidence_id or f"evidence:{index:016x}"
    claim_ref = claim_id or f"claim:{index:016x}"
    relation_ref = relation_id or f"relation:{index:016x}"
    assumptions = (
        ResearchAssumption(
            assumption_id=assumption_id,
            assumption_text=assumption_text,
            reason="declared_uncertainty",
            claim_ref=claim_ref,
        ),
    ) if assumption_id else ()
    assumption_refs = (assumption_id,) if assumption_id else ()
    return ResearchRecord(
        record_id=f"research_record:{index:016x}",
        record_digest="sha256:" + f"{index:064x}",
        question=ResearchQuestion(
            question_id=f"question:{index:016x}",
            question_text_digest="sha256:" + f"{index + 1:064x}",
        ),
        answer_status="answered",
        sources=(ResearchSource(
            source_id=source_ref,
            requested_url_ref=source_requested_url_ref or {},
            final_url_ref=source_final_url_ref or {},
            host=source_host,
            retrieved_at=source_retrieved_at,
            content_kind="html",
            page_count=source_page_count,
            pages_read=source_pages_read,
            truncated=source_truncated,
            quality=source_quality or {},
        ),),
        evidence=(ResearchEvidence(
            evidence_id=evidence_ref,
            source_id=source_ref,
            excerpt_digest="sha256:" + "e" * 64,
            bounded_excerpt=evidence_excerpt,
            locator=EvidenceLocator(
                kind="html",
                source_id=source_ref,
                char_start=0,
                char_end=len(evidence_excerpt),
            ),
            stance="supports",
            claim_text_digest="sha256:" + "c" * 64,
        ),),
        claims=(ResearchClaim(
            claim_id=claim_ref,
            claim_text=claim_text,
            claim_section="conclusion",
            evidence_refs=(evidence_ref,),
            assumption_refs=assumption_refs,
            status="evidence_backed",
        ),),
        assumptions=assumptions,
        relations=(ResearchClaimRelation(
            relation_id=relation_ref,
            relation_kind=relation_kind,
            from_ref=claim_ref,
            to_ref=evidence_ref,
        ),),
        run_id=f"run-{index}",
        session_id="session-ledger",
        stop_reason="done",
    )


def _assert_ledger_records_are_closed(payload: dict[str, object]) -> None:
    maps = {
        "source_refs": set(payload["sources"]),
        "evidence_refs": set(payload["evidence"]),
        "claim_refs": set(payload["claims"]),
        "assumption_refs": set(payload["assumptions"]),
        "relation_refs": set(payload["relations"]),
    }
    for record in payload["records"]:
        for key, known in maps.items():
            missing = set(record.get(key, ())) - known
            assert not missing, (record.get("record_id"), key, missing)
    source_ids = set(payload["sources"])
    claim_ids = set(payload["claims"])
    evidence_ids = set(payload["evidence"])
    assumption_ids = set(payload["assumptions"])
    for evidence in payload["evidence"].values():
        assert evidence["source_id"] in source_ids
    for claim in payload["claims"].values():
        assert not (set(claim.get("evidence_refs", ())) - evidence_ids)
        assert not (set(claim.get("assumption_refs", ())) - assumption_ids)
    for assumption in payload["assumptions"].values():
        if assumption.get("claim_ref"):
            assert assumption["claim_ref"] in claim_ids
    for relation in payload["relations"].values():
        assert relation["from_ref"] in claim_ids
        assert relation["to_ref"] in evidence_ids | assumption_ids


def _tamper_ledger(mutator) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record(project=project)
        assert store.append_record(
            record,
            run_id="run-ledger",
            session_id="session-ledger",
            project=project,
        ).ok
        path = store.path_for("session-ledger", project)
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutator(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = store.load(session_id="session-ledger", project=project)

    return snapshot.available, snapshot.reason_code


def test_tampered_record_entry_fails_closed_on_load() -> None:
    # Content addressing must hold on read, not only at write time: editing
    # the stored JSON by hand invalidates the whole ledger instead of
    # serving silently rewritten history.
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record(project=project)
        assert store.append_record(
            record,
            run_id="run-ledger",
            session_id="session-ledger",
            project=project,
        ).ok
        path = store.path_for("session-ledger", project)
        assert store.load(session_id="session-ledger", project=project).available

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][0]["answer_status"] = "answered"
        payload["records"][0]["counts"]["claims"] += 1
        path.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = store.load(session_id="session-ledger", project=project)

        assert not snapshot.available
        assert snapshot.reason_code == "ledger_unavailable"


def test_tampered_record_capsule_maps_fail_closed_on_load() -> None:
    mutators = (
        lambda payload: next(iter(payload["sources"].values())).__setitem__(
            "host",
            "other.example",
        ),
        lambda payload: next(iter(payload["evidence"].values())).__setitem__(
            "bounded_excerpt",
            "Different bounded excerpt.",
        ),
        lambda payload: next(iter(payload["claims"].values())).__setitem__(
            "status",
            "unsupported",
        ),
        lambda payload: next(iter(payload["assumptions"].values())).__setitem__(
            "reason",
            "different_reason",
        ),
        lambda payload: next(iter(payload["relations"].values())).__setitem__(
            "relation_kind",
            "conflicts_with",
        ),
    )

    for mutator in mutators:
        available, reason_code = _tamper_ledger(mutator)

        assert available is False
        assert reason_code == "ledger_unavailable"


def test_missing_record_digest_is_rejected_before_empty_digest_can_persist() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_home = Path(td) / "state"
        store = EvidenceLedgerStore(state_home)
        result = store.append_record(
            replace(_record(), record_digest=""),
            session_id="session-ledger",
        )
        path = store.path_for("session-ledger", None)

    assert result.skipped is True
    assert result.reason_code == "invalid_record"
    assert not path.exists()


def test_append_load_and_duplicate_are_bounded_and_deterministic() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record(project=project)

        first = store.append_record(
            record,
            run_id="run-ledger",
            session_id="session-ledger",
            project=project,
        )
        second = store.append_record(
            record,
            run_id="run-ledger",
            session_id="session-ledger",
            project=project,
        )
        snapshot = store.load(session_id="session-ledger", project=project)

    assert first.ok is True
    assert first.skipped is False
    assert first.ledger_ref.startswith("evidence_ledger:")
    assert first.record_id == record.record_id
    assert first.counts == {
        "records": 1,
        "sources": 1,
        "evidence": 1,
        "claims": 3,
        "assumptions": 1,
        "relations": 3,
    }
    assert second.ok is True
    assert second.skipped is True
    assert second.reason_code == "duplicate_record"
    assert snapshot.available is True
    assert snapshot.payload["schema_version"] == EVIDENCE_LEDGER_SCHEMA_VERSION
    assert snapshot.payload["kind"] == EVIDENCE_LEDGER_KIND
    assert len(snapshot.payload["records"]) == 1


def test_append_id_collision_skips_new_record_and_preserves_existing_payload() -> None:
    base = _capsule_record(30)
    source_id = base.sources[0].source_id
    evidence_id = base.evidence[0].evidence_id
    claim_id = base.claims[0].claim_id
    relation_id = base.relations[0].relation_id
    assumption_base = _capsule_record(
        40,
        assumption_id="assumption:" + "4" * 16,
    )
    assumption_id = assumption_base.assumptions[0].assumption_id
    cases = (
        (
            "source",
            base,
            _capsule_record(31, source_id=source_id, source_host="evil.example"),
        ),
        (
            "evidence",
            base,
            _capsule_record(
                32,
                source_id=source_id,
                evidence_id=evidence_id,
                evidence_excerpt="altered bounded excerpt",
            ),
        ),
        (
            "claim",
            base,
            _capsule_record(
                33,
                source_id=source_id,
                evidence_id=evidence_id,
                claim_id=claim_id,
                claim_text="Altered claim",
            ),
        ),
        (
            "relation",
            base,
            _capsule_record(
                34,
                source_id=source_id,
                evidence_id=evidence_id,
                claim_id=claim_id,
                relation_id=relation_id,
                relation_kind="limits",
            ),
        ),
        (
            "assumption",
            assumption_base,
            _capsule_record(
                41,
                source_id=assumption_base.sources[0].source_id,
                evidence_id=assumption_base.evidence[0].evidence_id,
                claim_id=assumption_base.claims[0].claim_id,
                assumption_id=assumption_id,
                assumption_text="Altered assumption",
                relation_id=assumption_base.relations[0].relation_id,
            ),
        ),
    )
    for label, first_record, colliding_record in cases:
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceLedgerStore(Path(td) / "state")
            first = store.append_record(first_record, session_id="session-ledger")
            path = store.path_for("session-ledger", None)
            before_text = path.read_text(encoding="utf-8")

            second = store.append_record(colliding_record, session_id="session-ledger")
            after_text = path.read_text(encoding="utf-8")
            snapshot = store.load(session_id="session-ledger")

        assert first.ok is True, label
        assert second.ok is False, label
        assert second.skipped is True, label
        assert second.reason_code == "ledger_id_collision", label
        assert second.counts == first.counts, label
        assert before_text == after_text, label
        assert snapshot.available is True, label
        assert len(snapshot.payload["records"]) == 1, label
        assert snapshot.payload["records"][0]["record_id"] == first_record.record_id, label


def test_append_same_source_identity_merges_observations_without_collision() -> None:
    source_id = "source:" + "5" * 16
    first_record = _capsule_record(
        50,
        source_id=source_id,
        source_requested_url_ref={
            "url_digest": "sha256:" + "1" * 64,
            "host": "mirror-a.example",
            "scheme": "https",
        },
        source_final_url_ref={
            "url_digest": "sha256:" + "2" * 64,
            "host": "example.com",
            "scheme": "https",
        },
        source_retrieved_at="2026-01-01T00:00:00Z",
        source_page_count=2,
        source_pages_read=(1,),
        source_quality={
            "level": "secondary",
            "kind": "web",
            "freshness": "stale",
            "independent_group": "example.com",
        },
    )
    second_record = _capsule_record(
        51,
        source_id=source_id,
        source_requested_url_ref={
            "url_digest": "sha256:" + "3" * 64,
            "host": "mirror-b.example",
            "scheme": "https",
        },
        source_final_url_ref={
            "url_digest": "sha256:" + "2" * 64,
            "host": "example.com",
            "scheme": "https",
        },
        source_retrieved_at="2026-01-02T00:00:00Z",
        source_page_count=3,
        source_pages_read=(2, 1),
        source_truncated=True,
        source_quality={
            "level": "primary",
            "kind": "official",
            "freshness": "fresh",
            "independent_group": "example.com",
        },
    )
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        first = store.append_record(first_record, session_id="session-ledger")
        second = store.append_record(second_record, session_id="session-ledger")
        snapshot = store.load(session_id="session-ledger")

    assert first.ok is True
    assert second.ok is True
    assert snapshot.available is True
    assert len(snapshot.payload["records"]) == 2
    source = snapshot.payload["sources"][source_id]
    assert source["retrieved_at"] == "2026-01-02T00:00:00Z"
    assert source["requested_url_ref"]["host"] == "mirror-a.example"
    assert source["final_url_ref"]["host"] == "example.com"
    assert source["page_count"] == 3
    assert source["pages_read"] == [1, 2]
    assert source["truncated"] is True
    assert source["quality"] == {
        "level": "secondary",
        "kind": "web",
        "freshness": "stale",
        "independent_group": "example.com",
    }


def test_append_same_source_id_with_different_final_url_ref_collides() -> None:
    source_id = "source:" + "6" * 16
    first_record = _capsule_record(
        60,
        source_id=source_id,
        source_final_url_ref={
            "url_digest": "sha256:" + "6" * 64,
            "host": "example.com",
            "scheme": "https",
        },
    )
    colliding_record = _capsule_record(
        61,
        source_id=source_id,
        source_final_url_ref={
            "url_digest": "sha256:" + "7" * 64,
            "host": "example.com",
            "scheme": "https",
        },
    )
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        first = store.append_record(first_record, session_id="session-ledger")
        path = store.path_for("session-ledger", None)
        before_text = path.read_text(encoding="utf-8")
        second = store.append_record(colliding_record, session_id="session-ledger")
        after_text = path.read_text(encoding="utf-8")

    assert first.ok is True
    assert second.skipped is True
    assert second.reason_code == "ledger_id_collision"
    assert before_text == after_text


def test_mapping_record_is_rejected_before_nested_refs_can_persist() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_home = Path(td) / "state"
        store = EvidenceLedgerStore(state_home)
        result = store.append_record({  # type: ignore[arg-type]
            "schema_version": 1,
            "kind": "research_record",
            "record_id": "research_record:" + "a" * 16,
            "record_digest": "sha256:" + "a" * 64,
            "sources": [{
                "source_id": "source:bad",
                "requested_url_ref": {
                    "raw": "https://api.example.com/items?token=SECRET_TOKEN",
                },
                "quality": {"raw_body": "SECRET_BODY"},
            }],
        })
        path = store.path_for("global", None)

    assert result.skipped is True
    assert result.reason_code == "invalid_record"
    assert not path.exists()


def test_ledger_persists_refs_without_raw_urls_paths_report_or_source_bodies() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "Sensitive Project"
        project.mkdir()
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record(project=project)

        result = store.append_record(
            record,
            run_id="run-ledger",
            session_id="session-ledger",
            project=project,
        )
        payload = json.loads(
            store.path_for("session-ledger", project).read_text(encoding="utf-8")
        )
        serialized = json.dumps(payload, ensure_ascii=False)

    assert result.ok is True
    assert "https://user:password@example.com" not in serialized
    assert "SECRET_TOKEN" not in serialized
    assert "Secret Source Title" not in serialized
    assert "PRIVATE_SOURCE_BODY_SENTINEL" not in serialized
    assert "SECRET_QUESTION_TOKEN" not in serialized
    assert str(project) not in serialized
    assert "Helium supply depends on gas processing." not in serialized
    assert "Helium is separated from natural gas streams." in serialized
    evidence_entry = next(iter(payload["evidence"].values()))
    locator = evidence_entry["locator"]
    assert locator["locator_id"].startswith("locator:")
    assert locator["locator_hash"].startswith("locator_span:")
    assert "text_hash" not in locator


def test_trim_keeps_retained_records_graph_closed_under_cap_pressure() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        latest = None
        for index in range(30):
            latest = _wide_record(index)
            result = store.append_record(latest, session_id="session-ledger")
            assert result.ok is True
        snapshot = store.load(session_id="session-ledger")
        payload = dict(snapshot.payload)

    assert latest is not None
    assert snapshot.available is True
    assert len(payload["evidence"]) <= MAX_LEDGER_EVIDENCE
    assert payload["records"][-1]["record_id"] == latest.record_id
    assert "records_pruned_for_ledger_closure" in payload["warnings"]
    _assert_ledger_records_are_closed(payload)


def test_load_rejects_existing_ledger_with_dangling_record_refs() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        path = store.path_for("session-ledger", None)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "schema_version": EVIDENCE_LEDGER_SCHEMA_VERSION,
                "kind": EVIDENCE_LEDGER_KIND,
                "ledger_ref": "evidence_ledger:" + "a" * 16,
                "records": [{
                    "record_id": "research_record:" + "a" * 16,
                    "record_digest": "sha256:" + "a" * 64,
                    "source_refs": ["source:missing"],
                    "evidence_refs": [],
                    "claim_refs": [],
                    "assumption_refs": [],
                    "relation_refs": [],
                }],
                "sources": {},
                "evidence": {},
                "claims": {},
                "assumptions": {},
                "relations": {},
            }),
            encoding="utf-8",
        )

        snapshot = store.load(session_id="session-ledger")
        append_result = store.append_record(_record(), session_id="session-ledger")

    assert snapshot.available is False
    assert snapshot.reason_code == "ledger_unavailable"
    assert append_result.skipped is True
    assert append_result.reason_code == "ledger_unavailable"


def test_load_rejects_closed_ledger_with_unknown_raw_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record()
        result = store.append_record(record, session_id="session-ledger")
        assert result.ok is True
        path = store.path_for("session-ledger", None)
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_id = payload["records"][0]["source_refs"][0]
        evidence_id = payload["records"][0]["evidence_refs"][0]
        claim_id = payload["records"][0]["claim_refs"][0]
        payload["raw_prompt"] = "SECRET_RAW_PROMPT"
        payload["sources"][source_id]["raw_url"] = "https://example.com?token=SECRET"
        payload["evidence"][evidence_id]["raw_body"] = "SECRET_RAW_BODY"
        payload["claims"][claim_id]["provider_raw_error"] = "SECRET_PROVIDER_ERROR"
        path.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = store.load(session_id="session-ledger")
        append_result = store.append_record(_record(), session_id="session-ledger")

    assert snapshot.available is False
    assert snapshot.reason_code == "ledger_unavailable"
    assert append_result.skipped is True
    assert append_result.reason_code == "ledger_unavailable"


def test_load_rejects_orphan_map_entries() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record()
        result = store.append_record(record, session_id="session-ledger")
        assert result.ok is True
        path = store.path_for("session-ledger", None)
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence_id = payload["records"][0]["evidence_refs"][0]
        orphan = dict(payload["evidence"][evidence_id])
        orphan["evidence_id"] = "evidence:orphan"
        payload["evidence"]["evidence:orphan"] = orphan
        path.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = store.load(session_id="session-ledger")

    assert snapshot.available is False
    assert snapshot.reason_code == "ledger_unavailable"


def test_load_rejects_map_key_entry_id_mismatch() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record()
        result = store.append_record(record, session_id="session-ledger")
        assert result.ok is True
        path = store.path_for("session-ledger", None)
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_id = payload["records"][0]["source_refs"][0]
        payload["sources"][source_id]["source_id"] = "source:wrong"
        path.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = store.load(session_id="session-ledger")

    assert snapshot.available is False
    assert snapshot.reason_code == "ledger_unavailable"


def test_load_rejects_known_fields_with_noncanonical_or_unbounded_values() -> None:
    mutations = (
        ("top_level_ledger_ref", lambda payload: payload.update({"ledger_ref": "SECRET_LEDGER_REF"})),
        ("top_level_session_ref", lambda payload: payload.update({"session_ref": "SECRET_SESSION_REF"})),
        ("warning_code", lambda payload: payload.update({"warnings": ["SECRET_WARNING_TOKEN"]})),
        (
            "record_session_ref",
            lambda payload: payload["records"][0].update({"session_ref": "SECRET_RECORD_SESSION"}),
        ),
        (
            "source_host",
            lambda payload: payload["sources"][payload["records"][0]["source_refs"][0]].update({
                "host": "SECRET_HOST_TOKEN",
            }),
        ),
        (
            "evidence_excerpt",
            lambda payload: payload["evidence"][payload["records"][0]["evidence_refs"][0]].update({
                "bounded_excerpt": "SECRET_BODY_" + ("x" * 500),
            }),
        ),
        (
            "claim_status",
            lambda payload: payload["claims"][payload["records"][0]["claim_refs"][0]].update({
                "status": "SECRET_STATUS",
            }),
        ),
        (
            "evidence_stance",
            lambda payload: payload["evidence"][payload["records"][0]["evidence_refs"][0]].update({
                "stance": "SECRET_STANCE",
            }),
        ),
    )
    for label, mutate in mutations:
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceLedgerStore(Path(td) / "state")
            record = _record()
            result = store.append_record(record, session_id="session-ledger")
            assert result.ok is True, label
            path = store.path_for("session-ledger", None)
            payload = json.loads(path.read_text(encoding="utf-8"))
            mutate(payload)
            path.write_text(json.dumps(payload), encoding="utf-8")

            snapshot = store.load(session_id="session-ledger")

        assert snapshot.available is False, label
        assert snapshot.reason_code == "ledger_unavailable", label


def test_load_rejects_record_counts_that_do_not_match_refs() -> None:
    mutations = (
        ("sources_count", lambda payload: payload["records"][0]["counts"].update({"sources": 999999})),
        ("evidence_count", lambda payload: payload["records"][0]["counts"].update({"evidence": 0})),
        (
            "unsupported_claim_count",
            lambda payload: payload["records"][0]["counts"].update({"unsupported_claims": 999999}),
        ),
    )
    for label, mutate in mutations:
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceLedgerStore(Path(td) / "state")
            record = _record()
            result = store.append_record(record, session_id="session-ledger")
            assert result.ok is True, label
            path = store.path_for("session-ledger", None)
            payload = json.loads(path.read_text(encoding="utf-8"))
            mutate(payload)
            path.write_text(json.dumps(payload), encoding="utf-8")

            snapshot = store.load(session_id="session-ledger")

        assert snapshot.available is False, label
        assert snapshot.reason_code == "ledger_unavailable", label


def test_malformed_typed_source_content_hash_does_not_persist_raw_value() -> None:
    for index, content_hash in enumerate(("SECRET_CONTENT_HASH", "sha256:SECRET_CONTENT_HASH"), start=6):
        record = ResearchRecord(
            record_id=f"research_record:{index:016x}",
            record_digest="sha256:" + f"{index:064x}",
            question=ResearchQuestion(
                question_id=f"question:{index:016x}",
                question_text_digest="sha256:" + f"{index + 1:064x}",
            ),
            answer_status="answered",
            sources=(ResearchSource(
                source_id=f"source:{index:016x}",
                content_hash=content_hash,
            ),),
        )
        with tempfile.TemporaryDirectory() as td:
            store = EvidenceLedgerStore(Path(td) / "state")
            result = store.append_record(record, session_id="session-ledger")
            path = store.path_for("session-ledger", None)
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot = store.load(session_id="session-ledger")
            serialized = json.dumps(payload)

        assert result.ok is True
        assert "SECRET_CONTENT_HASH" not in serialized
        assert payload["sources"][f"source:{index:016x}"]["content_hash"] == ""
        assert snapshot.available is True


def test_noncanonical_typed_record_candidate_is_not_written() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        good = _wide_record(20, evidence_count=1)
        first = store.append_record(good, session_id="session-ledger")
        assert first.ok is True
        source_id = "source:" + "7" * 16
        evidence_id = "evidence:" + "7" * 16
        claim_id = "claim:" + "7" * 16
        bad = ResearchRecord(
            record_id="research_record:" + "7" * 16,
            record_digest="sha256:" + "7" * 64,
            question=ResearchQuestion(
                question_id="question:" + "7" * 16,
                question_text_digest="sha256:" + "8" * 64,
            ),
            answer_status="answered",
            sources=(ResearchSource(
                source_id=source_id,
                host="SECRET_HOST",
            ),),
            evidence=(ResearchEvidence(
                evidence_id=evidence_id,
                source_id=source_id,
                excerpt_digest="sha256:" + "9" * 64,
                bounded_excerpt="bounded excerpt",
                locator=EvidenceLocator(kind="html", source_id=source_id),
                stance="SECRET_STANCE",
            ),),
            claims=(ResearchClaim(
                claim_id=claim_id,
                claim_text="Bad canonical candidate",
                claim_section="conclusion",
                evidence_refs=(evidence_id,),
                status="SECRET_STATUS",
            ),),
            relations=(ResearchClaimRelation(
                relation_id="relation:" + "7" * 16,
                relation_kind="supports",
                from_ref=claim_id,
                to_ref=evidence_id,
            ),),
        )

        second = store.append_record(bad, session_id="session-ledger")
        snapshot = store.load(session_id="session-ledger")
        serialized = store.path_for("session-ledger", None).read_text(encoding="utf-8")

    assert second.skipped is True
    assert second.reason_code == "invalid_record"
    assert second.counts == first.counts
    assert snapshot.available is True
    assert len(snapshot.payload["records"]) == 1
    assert snapshot.payload["records"][0]["record_id"] == good.record_id
    assert "SECRET_HOST" not in serialized
    assert "SECRET_STANCE" not in serialized
    assert "SECRET_STATUS" not in serialized


def test_malformed_typed_record_with_nested_dangling_refs_is_pruned() -> None:
    record = ResearchRecord(
        record_id="research_record:" + "1" * 16,
        record_digest="sha256:" + "1" * 64,
        question=ResearchQuestion(
            question_id="question:" + "1" * 16,
            question_text_digest="sha256:" + "2" * 64,
        ),
        answer_status="answered",
        sources=(ResearchSource(source_id="source:" + "1" * 16),),
        claims=(ResearchClaim(
            claim_id="claim:" + "1" * 16,
            claim_text="Malformed claim",
            claim_section="conclusion",
            evidence_refs=("evidence:missing",),
            assumption_refs=("assumption:" + "1" * 16,),
            status="evidence_backed",
        ),),
        assumptions=(ResearchAssumption(
            assumption_id="assumption:" + "1" * 16,
            assumption_text="Malformed assumption",
            claim_ref="claim:missing",
        ),),
    )
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        result = store.append_record(record, session_id="session-ledger")
        path = store.path_for("session-ledger", None)

    assert result.ok is False
    assert result.skipped is True
    assert result.reason_code == "record_pruned_for_ledger_closure"
    assert result.record_id == record.record_id
    assert result.counts == {}
    assert not path.exists()


def test_malformed_typed_record_with_dangling_locator_source_is_pruned() -> None:
    source_id = "source:" + "3" * 16
    evidence_id = "evidence:" + "3" * 16
    claim_id = "claim:" + "3" * 16
    record = ResearchRecord(
        record_id="research_record:" + "3" * 16,
        record_digest="sha256:" + "3" * 64,
        question=ResearchQuestion(
            question_id="question:" + "3" * 16,
            question_text_digest="sha256:" + "4" * 64,
        ),
        answer_status="answered",
        sources=(ResearchSource(source_id=source_id),),
        evidence=(ResearchEvidence(
            evidence_id=evidence_id,
            source_id=source_id,
            excerpt_digest="sha256:" + "5" * 64,
            bounded_excerpt="bounded excerpt",
            locator=EvidenceLocator(
                kind="html",
                source_id="source:missinglocator",
                char_start=0,
                char_end=10,
            ),
        ),),
        claims=(ResearchClaim(
            claim_id=claim_id,
            claim_text="Malformed locator source",
            claim_section="conclusion",
            evidence_refs=(evidence_id,),
            status="evidence_backed",
        ),),
        relations=(ResearchClaimRelation(
            relation_id="relation:" + "3" * 16,
            relation_kind="supports",
            from_ref=claim_id,
            to_ref=evidence_id,
        ),),
    )
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        result = store.append_record(record, session_id="session-ledger")
        path = store.path_for("session-ledger", None)

    assert result.skipped is True
    assert result.reason_code == "record_pruned_for_ledger_closure"
    assert not path.exists()


def test_malformed_typed_record_with_mismatched_locator_source_is_pruned() -> None:
    source_a = "source:" + "4" * 16
    source_b = "source:" + "5" * 16
    evidence_id = "evidence:" + "4" * 16
    claim_id = "claim:" + "4" * 16
    record = ResearchRecord(
        record_id="research_record:" + "4" * 16,
        record_digest="sha256:" + "4" * 64,
        question=ResearchQuestion(
            question_id="question:" + "4" * 16,
            question_text_digest="sha256:" + "5" * 64,
        ),
        answer_status="answered",
        sources=(
            ResearchSource(source_id=source_a),
            ResearchSource(source_id=source_b),
        ),
        evidence=(ResearchEvidence(
            evidence_id=evidence_id,
            source_id=source_a,
            excerpt_digest="sha256:" + "6" * 64,
            bounded_excerpt="bounded excerpt",
            locator=EvidenceLocator(
                kind="html",
                source_id=source_b,
                char_start=0,
                char_end=10,
            ),
        ),),
        claims=(ResearchClaim(
            claim_id=claim_id,
            claim_text="Mismatched locator source",
            claim_section="conclusion",
            evidence_refs=(evidence_id,),
            status="evidence_backed",
        ),),
        relations=(ResearchClaimRelation(
            relation_id="relation:" + "4" * 16,
            relation_kind="supports",
            from_ref=claim_id,
            to_ref=evidence_id,
        ),),
    )
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        result = store.append_record(record, session_id="session-ledger")
        path = store.path_for("session-ledger", None)

    assert result.skipped is True
    assert result.reason_code == "record_pruned_for_ledger_closure"
    assert not path.exists()


def test_pruned_record_with_existing_same_id_returns_existing_ledger_counts() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        good = _wide_record(10, evidence_count=1)
        first = store.append_record(good, session_id="session-ledger")
        bad = ResearchRecord(
            record_id=good.record_id,
            record_digest="sha256:" + "f" * 64,
            question=good.question,
            answer_status="answered",
            claims=(ResearchClaim(
                claim_id="claim:bad_same_id",
                claim_text="Bad same id",
                claim_section="conclusion",
                evidence_refs=("evidence:missing",),
                status="evidence_backed",
            ),),
        )

        second = store.append_record(bad, session_id="session-ledger")
        snapshot = store.load(session_id="session-ledger")

    assert first.ok is True
    assert second.skipped is True
    assert second.reason_code == "record_pruned_for_ledger_closure"
    assert second.counts == first.counts
    assert snapshot.available is True
    assert snapshot.payload["records"][0]["record_id"] == good.record_id
    assert snapshot.payload["records"][0]["record_digest"] == good.record_digest


def test_pruned_replacement_does_not_delete_existing_good_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        replaced = _wide_record(12, evidence_count=1)
        other = _wide_record(13, evidence_count=1)
        first = store.append_record(replaced, session_id="session-ledger")
        second_good = store.append_record(other, session_id="session-ledger")
        bad = ResearchRecord(
            record_id=replaced.record_id,
            record_digest="sha256:" + "e" * 64,
            question=replaced.question,
            answer_status="answered",
            claims=(ResearchClaim(
                claim_id="claim:bad_replacement",
                claim_text="Bad replacement",
                claim_section="conclusion",
                evidence_refs=("evidence:missing",),
                status="evidence_backed",
            ),),
        )

        skipped = store.append_record(bad, session_id="session-ledger")
        snapshot = store.load(session_id="session-ledger")

    assert first.ok is True
    assert second_good.ok is True
    assert skipped.skipped is True
    assert skipped.reason_code == "record_pruned_for_ledger_closure"
    assert skipped.counts == second_good.counts
    assert snapshot.available is True
    rows = {item["record_id"]: item for item in snapshot.payload["records"]}
    assert set(rows) == {replaced.record_id, other.record_id}
    assert rows[replaced.record_id]["record_digest"] == replaced.record_digest
    assert rows[other.record_id]["record_digest"] == other.record_digest
    _assert_ledger_records_are_closed(dict(snapshot.payload))


def test_malformed_typed_record_to_json_failure_returns_invalid_record() -> None:
    record = ResearchRecord(
        record_id="research_record:" + "2" * 16,
        record_digest="sha256:" + "2" * 64,
        question=ResearchQuestion(
            question_id="question:" + "2" * 16,
            question_text_digest="sha256:" + "3" * 64,
        ),
        answer_status="answered",
        sources=(object(),),  # type: ignore[arg-type]
    )
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        result = store.append_record(record, session_id="session-ledger")
        path = store.path_for("session-ledger", None)

    assert result.ok is False
    assert result.skipped is True
    assert result.reason_code == "invalid_record"
    assert result.record_id == record.record_id
    assert not path.exists()


def test_bad_or_oversized_ledger_is_unavailable_and_append_fails_open() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record()
        path = store.path_for("session-ledger", None)
        path.parent.mkdir(parents=True)
        path.write_text("{bad json", encoding="utf-8")

        snapshot = store.load(session_id="session-ledger")
        result = store.append_record(record, session_id="session-ledger")

        path.write_text("x" * (MAX_EVIDENCE_LEDGER_BYTES + 1), encoding="utf-8")
        oversized = store.load(session_id="session-ledger")

    assert snapshot.available is False
    assert snapshot.reason_code == "ledger_unavailable"
    assert result.skipped is True
    assert result.reason_code == "ledger_unavailable"
    assert result.record_id == record.record_id
    assert oversized.available is False
    assert oversized.reason_code == "ledger_unavailable"


def test_write_failure_returns_skipped_without_raising() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record()

        with mock.patch("codey.research.evidence_ledger.write_json_atomic", side_effect=OSError("disk")):
            result = store.append_record(record, session_id="session-ledger")

    assert result.ok is False
    assert result.skipped is True
    assert result.reason_code == "write_failed"
    assert result.record_id == record.record_id
