"""Durable bounded Research evidence read model."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from codey.storage.local_store import read_json, session_key, write_json_atomic
from codey.storage.transactional_json import with_file_lock
from codey.utils.refs import (
    bounded_refs,
    clip,
    content_digest,
    digest_text,
    identifier,
    nonnegative_int,
    stable_ref,
)
from codey.research.identity import project_ref
from codey.research.object_model import (
    ANSWER_STATUSES,
    CLAIM_RELATION_KINDS,
    CLAIM_STATUSES,
    MAX_CLAIM_TEXT_CHARS,
    RESEARCH_RECORD_KIND,
    RESEARCH_RECORD_SCHEMA_VERSION,
    ResearchRecord,
)


EVIDENCE_LEDGER_SCHEMA_VERSION = 1
EVIDENCE_LEDGER_KIND = "research_evidence_ledger"
MAX_EVIDENCE_LEDGER_BYTES = 1024 * 1024
MAX_LEDGER_RECORDS = 100
MAX_LEDGER_SOURCES = 300
MAX_LEDGER_EVIDENCE = 800
MAX_LEDGER_CLAIMS = 800
MAX_LEDGER_ASSUMPTIONS = 800
MAX_LEDGER_RELATIONS = 1600
MAX_WARNINGS = 16

_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "kind",
    "ledger_ref",
    "session_ref",
    "project_ref",
    "updated_at",
    "records",
    "sources",
    "evidence",
    "claims",
    "assumptions",
    "relations",
    "warnings",
})
_RECORD_KEYS = frozenset({
    "record_id",
    "record_digest",
    "run_id",
    "session_ref",
    "project_ref",
    "answer_status",
    "source_refs",
    "evidence_refs",
    "claim_refs",
    "assumption_refs",
    "relation_refs",
    "counts",
    "synthesis_id",
    "stop_reason",
    "captured_at",
    "record_integrity",
})
_SOURCE_KEYS = frozenset({
    "source_id",
    "requested_url_ref",
    "final_url_ref",
    "host",
    "title_digest",
    "content_hash",
    "retrieved_at",
    "content_kind",
    "page_count",
    "pages_read",
    "truncated",
    "quality",
})
_EVIDENCE_KEYS = frozenset({
    "evidence_id",
    "source_id",
    "excerpt_digest",
    "bounded_excerpt",
    "locator",
    "stance",
    "note_id",
    "claim_text_digest",
})
_CLAIM_KEYS = frozenset({
    "claim_id",
    "claim_text_digest",
    "claim_chars",
    "claim_section",
    "citation_numbers",
    "evidence_refs",
    "assumption_refs",
    "status",
})
_ASSUMPTION_KEYS = frozenset({
    "assumption_id",
    "assumption_text_digest",
    "assumption_chars",
    "reason",
    "claim_ref",
})
_RELATION_KEYS = frozenset({
    "relation_id",
    "relation_kind",
    "from_ref",
    "to_ref",
    "citation_numbers",
})
_LOCATOR_KEYS = frozenset({
    "locator_id",
    "kind",
    "source_id",
    "page",
    "char_start",
    "char_end",
    "locator",
    "locator_hash",
})
_URL_REF_KEYS = frozenset({"url_digest", "path_digest", "host", "scheme", "redacted"})
_PROJECT_REF_KEYS = frozenset({"basename", "digest"})
_QUALITY_KEYS = frozenset({"level", "kind", "freshness", "independent_group"})
_COUNT_KEYS = frozenset({
    "sources",
    "evidence",
    "claims",
    "assumptions",
    "relations",
    "unsupported_claims",
})
_REF_LIST_KEYS = frozenset({
    "source_refs",
    "evidence_refs",
    "claim_refs",
    "assumption_refs",
    "relation_refs",
})
_CAPSULE_MAP_KEYS = ("sources", "evidence", "claims", "assumptions", "relations")
_SOURCE_FIXED_IDENTITY_KEYS = frozenset({
    "source_id",
    "host",
    "content_hash",
    "content_kind",
})


@dataclass(frozen=True)
class EvidenceLedgerWriteResult:
    ok: bool = False
    skipped: bool = False
    reason_code: str = ""
    ledger_ref: str = ""
    record_id: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_trace_payload(self) -> dict[str, object]:
        return {
            "ok": bool(self.ok),
            "skipped": bool(self.skipped),
            "reason_code": identifier(self.reason_code, 80),
            "ledger_ref": identifier(self.ledger_ref, 80),
            "record_id": _record_id(self.record_id),
            "counts": {
                key: max(0, int(value))
                for key, value in dict(self.counts).items()
                if isinstance(key, str)
            },
            "warnings": [identifier(item, 120) for item in self.warnings[:MAX_WARNINGS]],
        }


@dataclass(frozen=True)
class EvidenceLedgerSnapshot:
    available: bool
    path: Path
    payload: Mapping[str, object] = field(default_factory=dict)
    reason_code: str = ""

    @property
    def ledger_ref(self) -> str:
        return identifier(self.payload.get("ledger_ref") if self.payload else "", 80)


class EvidenceLedgerStore:
    def __init__(self, state_home: str | Path) -> None:
        if not state_home:
            raise ValueError("state_home required")
        self.root = Path(state_home) / "research" / "evidence_ledgers"

    def append_record(
        self,
        record: ResearchRecord | None,
        *,
        run_id: str = "",
        session_id: str = "",
        project: str | Path | None = None,
    ) -> EvidenceLedgerWriteResult:
        if record is None:
            return EvidenceLedgerWriteResult(skipped=True, reason_code="missing_record")
        try:
            record_payload = _record_payload(record)
        except Exception:
            return EvidenceLedgerWriteResult(
                skipped=True,
                reason_code="invalid_record",
                record_id=_record_id(getattr(record, "record_id", "")),
            )
        if not record_payload:
            return EvidenceLedgerWriteResult(
                skipped=True,
                reason_code="invalid_record",
                record_id=_record_id(getattr(record, "record_id", "")),
            )
        try:
            path = self.path_for(session_id, project)
            # The lock makes load→mutate→write one serialized transaction:
            # two concurrent appends each read the same version and both
            # write their own next version, so without it one record would
            # silently vanish from the ledger.
            with with_file_lock(path):
                loaded = self._load_payload(path)
                if loaded is None:
                    return EvidenceLedgerWriteResult(
                        skipped=True,
                        reason_code="ledger_unavailable",
                        record_id=str(record_payload.get("record_id") or ""),
                    )
                payload = loaded or self._new_payload(session_id=session_id, project=project)
                previous_counts = _counts(loaded) if loaded else {}
                previous_ledger_ref = str(loaded.get("ledger_ref") or "") if loaded else ""
                previous_warnings = tuple(
                    str(item) for item in (loaded or {}).get("warnings", ())[:MAX_WARNINGS]
                )
                record_id = str(record_payload.get("record_id") or "")
                record_digest = str(record_payload.get("record_digest") or "")
                if _has_record(payload, record_id, record_digest):
                    return EvidenceLedgerWriteResult(
                        ok=True,
                        skipped=True,
                        reason_code="duplicate_record",
                        ledger_ref=str(payload.get("ledger_ref") or ""),
                        record_id=record_id,
                        counts=_counts(payload),
                    )
                candidate = deepcopy(payload)
                appended = _append_payload(
                    candidate,
                    record_payload,
                    run_id=run_id,
                    session_id=session_id,
                    project=project,
                )
                if not appended:
                    return EvidenceLedgerWriteResult(
                        skipped=True,
                        reason_code="ledger_id_collision",
                        ledger_ref=previous_ledger_ref,
                        record_id=record_id,
                        counts=previous_counts,
                        warnings=previous_warnings,
                    )
                if not _has_record(candidate, record_id, record_digest):
                    return EvidenceLedgerWriteResult(
                        skipped=True,
                        reason_code="record_pruned_for_ledger_closure",
                        ledger_ref=previous_ledger_ref,
                        record_id=record_id,
                        counts=previous_counts,
                        warnings=previous_warnings,
                    )
                if not _canonical_ledger_payload(candidate):
                    return EvidenceLedgerWriteResult(
                        skipped=True,
                        reason_code="invalid_record",
                        ledger_ref=previous_ledger_ref,
                        record_id=record_id,
                        counts=previous_counts,
                        warnings=previous_warnings,
                    )
                payload = candidate
                write_json_atomic(path, payload, max_bytes=MAX_EVIDENCE_LEDGER_BYTES)
        except (OSError, RuntimeError, TypeError, ValueError):
            return EvidenceLedgerWriteResult(
                skipped=True,
                reason_code="write_failed",
                record_id=str(record_payload.get("record_id") or ""),
            )
        return EvidenceLedgerWriteResult(
            ok=True,
            ledger_ref=str(payload.get("ledger_ref") or ""),
            record_id=record_id,
            counts=_counts(payload),
            warnings=tuple(str(item) for item in payload.get("warnings", ())[:MAX_WARNINGS]),
        )

    def load(
        self,
        *,
        session_id: str = "",
        project: str | Path | None = None,
    ) -> EvidenceLedgerSnapshot:
        path = self.path_for(session_id, project)
        payload = self._load_payload(path)
        if payload is None:
            return EvidenceLedgerSnapshot(False, path, reason_code="ledger_unavailable")
        if not payload:
            return EvidenceLedgerSnapshot(False, path, reason_code="missing_ledger")
        return EvidenceLedgerSnapshot(True, path, payload=payload)

    def path_for(self, session_id: str = "", project: str | Path | None = None) -> Path:
        session_part = session_key(session_id or "global")
        project_digest = str(project_ref(project).get("digest") or "no_project").removeprefix("sha256:")
        project_part = identifier(project_digest[:24] or "no_project", 32)
        path = self.root / session_part / f"{project_part}.json"
        root = self.root.resolve()
        resolved = path.resolve()
        resolved.relative_to(root)
        return resolved

    def _load_payload(self, path: Path) -> dict[str, object] | None:
        if not path.exists():
            return {}
        payload = read_json(path, max_bytes=MAX_EVIDENCE_LEDGER_BYTES)
        if not _canonical_ledger_payload(payload):
            return None
        # Content-addressing must hold on read too, not only at write time:
        # any in-file tampering invalidates the whole ledger (fail closed)
        # instead of serving silently rewritten history.
        if not _records_integrity_ok(payload):
            return None
        return dict(payload)

    def _new_payload(self, *, session_id: str, project: str | Path | None) -> dict[str, object]:
        scope = {
            "session_ref": digest_text(session_id or "global"),
            "project_ref": project_ref(project),
        }
        return {
            "schema_version": EVIDENCE_LEDGER_SCHEMA_VERSION,
            "kind": EVIDENCE_LEDGER_KIND,
            "ledger_ref": stable_ref(
                "evidence_ledger",
                scope["session_ref"],
                scope["project_ref"],
            ),
            **scope,
            "updated_at": _now(),
            "records": [],
            "sources": {},
            "evidence": {},
            "claims": {},
            "assumptions": {},
            "relations": {},
            "warnings": [],
        }


def _record_payload(record: object) -> dict[str, object]:
    if not isinstance(record, ResearchRecord):
        return {}
    if not _digest_schema_ok(getattr(record, "record_digest", "")):
        return {}
    payload = record.to_jsonable()
    if (
        payload.get("schema_version") != RESEARCH_RECORD_SCHEMA_VERSION
        or payload.get("kind") != RESEARCH_RECORD_KIND
        or not _record_id(payload.get("record_id"))
        # A record without its own content digest is invalid input: storing
        # it would mint the empty-string digest (e3b0c442...) and look
        # legitimate while carrying no integrity at all.
        or not _digest_schema_ok(payload.get("record_digest"))
    ):
        return {}
    return payload


def _record_integrity(payload: Mapping[str, object], entry: Mapping[str, object]) -> str:
    """Content digest over one record capsule.

    The capsule includes the normalized record entry plus every normalized
    source/evidence/claim/assumption/relation row it references. Serialization
    is explicit (sorted keys, no whitespace), so the digest is stable across
    dict insertion order and JSON round-trips.
    """

    refs = _record_refs(entry)
    maps = {key: _mapping(payload.get(key)) for key in _CAPSULE_MAP_KEYS}
    capsule = {
        "record": {key: value for key, value in entry.items() if key != "record_integrity"},
        **{
            key: {item_id: maps[key].get(item_id, {}) for item_id in sorted(refs[key])}
            for key in _CAPSULE_MAP_KEYS
        },
    }
    return digest_text(json.dumps(capsule, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _records_integrity_ok(payload: Mapping[str, object]) -> bool:
    """Verify every stored record entry's integrity digest on load."""

    for entry in _records(payload):
        expected = str(entry.get("record_integrity") or "")
        if not expected:
            return False
        if _record_integrity(payload, entry) != expected:
            return False
    return True


def _append_payload(
    payload: dict[str, object],
    record: Mapping[str, object],
    *,
    run_id: str,
    session_id: str,
    project: str | Path | None,
) -> bool:
    record_id = _record_id(record.get("record_id"))
    sources = _dict_map(payload, "sources")
    evidence = _dict_map(payload, "evidence")
    claims = _dict_map(payload, "claims")
    assumptions = _dict_map(payload, "assumptions")
    relations = _dict_map(payload, "relations")
    source_ids: list[str] = []
    evidence_ids: list[str] = []
    claim_ids: list[str] = []
    assumption_ids: list[str] = []
    relation_ids: list[str] = []

    for item in _list(record.get("sources"))[:MAX_LEDGER_SOURCES]:
        source_id = identifier(item.get("source_id"), 80)
        if source_id:
            if not _put_source_row(sources, source_id, _source_entry(item)):
                return False
            source_ids.append(source_id)
    for item in _list(record.get("evidence"))[:MAX_LEDGER_EVIDENCE]:
        evidence_id = identifier(item.get("evidence_id"), 80)
        if evidence_id:
            if not _put_capsule_row(evidence, evidence_id, _evidence_entry(item)):
                return False
            evidence_ids.append(evidence_id)
    for item in _list(record.get("claims"))[:MAX_LEDGER_CLAIMS]:
        claim_id = identifier(item.get("claim_id"), 80)
        if claim_id:
            if not _put_capsule_row(claims, claim_id, _claim_entry(item)):
                return False
            claim_ids.append(claim_id)
    for item in _list(record.get("assumptions"))[:MAX_LEDGER_ASSUMPTIONS]:
        assumption_id = identifier(item.get("assumption_id"), 80)
        if assumption_id:
            if not _put_capsule_row(assumptions, assumption_id, _assumption_entry(item)):
                return False
            assumption_ids.append(assumption_id)
    for item in _list(record.get("relations"))[:MAX_LEDGER_RELATIONS]:
        relation_id = identifier(item.get("relation_id"), 80)
        if relation_id:
            if not _put_capsule_row(relations, relation_id, _relation_entry(item)):
                return False
            relation_ids.append(relation_id)

    record_entry = {
        "record_id": record_id,
        "record_digest": content_digest(record.get("record_digest")),
        "run_id": identifier(run_id or record.get("run_id"), 120),
        "session_ref": digest_text(session_id or record.get("session_id") or "global"),
        "project_ref": project_ref(project) or _project_ref_entry(record.get("project_ref")),
        "answer_status": identifier(record.get("answer_status"), 40),
        "source_refs": list(bounded_refs(source_ids, limit=MAX_LEDGER_SOURCES)),
        "evidence_refs": list(bounded_refs(evidence_ids, limit=MAX_LEDGER_EVIDENCE)),
        "claim_refs": list(bounded_refs(claim_ids, limit=MAX_LEDGER_CLAIMS)),
        "assumption_refs": list(bounded_refs(assumption_ids, limit=MAX_LEDGER_ASSUMPTIONS)),
        "relation_refs": list(bounded_refs(relation_ids, limit=MAX_LEDGER_RELATIONS)),
        "counts": {
            "sources": len(source_ids),
            "evidence": len(evidence_ids),
            "claims": len(claim_ids),
            "assumptions": len(assumption_ids),
            "relations": len(relation_ids),
            "unsupported_claims": nonnegative_int(record.get("unsupported_claim_count")),
        },
        "synthesis_id": identifier(record.get("synthesis_id"), 120),
        "stop_reason": identifier(record.get("stop_reason"), 80),
        "captured_at": _now(),
    }
    records = _records(payload)
    records = [item for item in records if item.get("record_id") != record_id]
    records.append(record_entry)
    payload["records"] = records[-MAX_LEDGER_RECORDS:]
    payload["updated_at"] = _now()
    _trim_maps(payload)
    # Stamp every surviving record after map normalization. Shared map rows
    # are part of each record capsule, so accepted source observations and
    # retained rows must leave all surviving capsules coherent on load.
    _stamp_record_integrities(payload)
    return True


def _stamp_record_integrities(payload: Mapping[str, object]) -> None:
    for record in _records(payload):
        record["record_integrity"] = _record_integrity(payload, record)


def _put_capsule_row(
    rows: dict[str, object],
    row_id: str,
    entry: Mapping[str, object],
) -> bool:
    existing = rows.get(row_id)
    normalized = dict(entry)
    if existing is not None and existing != normalized:
        return False
    rows[row_id] = normalized
    return True


def _put_source_row(
    rows: dict[str, object],
    row_id: str,
    entry: Mapping[str, object],
) -> bool:
    existing = rows.get(row_id)
    normalized = dict(entry)
    if existing is None:
        rows[row_id] = normalized
        return True
    if not isinstance(existing, dict):
        return False
    if not _source_rows_compatible(existing, normalized):
        return False
    rows[row_id] = _merge_source_entry(existing, normalized)
    return True


def _source_rows_compatible(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
) -> bool:
    for key in _SOURCE_FIXED_IDENTITY_KEYS:
        if existing.get(key) != incoming.get(key):
            return False
    existing_final = _mapping(existing.get("final_url_ref"))
    incoming_final = _mapping(incoming.get("final_url_ref"))
    return not (existing_final and incoming_final and existing_final != incoming_final)


def _merge_source_entry(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
) -> dict[str, object]:
    merged = dict(existing)
    if not _mapping(merged.get("requested_url_ref")) and _mapping(incoming.get("requested_url_ref")):
        merged["requested_url_ref"] = incoming.get("requested_url_ref")
    if not _mapping(merged.get("final_url_ref")) and _mapping(incoming.get("final_url_ref")):
        merged["final_url_ref"] = incoming.get("final_url_ref")
    if not merged.get("title_digest") and incoming.get("title_digest"):
        merged["title_digest"] = incoming.get("title_digest")
    if str(incoming.get("retrieved_at") or "") > str(merged.get("retrieved_at") or ""):
        merged["retrieved_at"] = incoming.get("retrieved_at")
    merged["page_count"] = max(
        nonnegative_int(merged.get("page_count")),
        nonnegative_int(incoming.get("page_count")),
    )
    merged["pages_read"] = _merge_positive_ints(
        merged.get("pages_read"),
        incoming.get("pages_read"),
        48,
    )
    merged["truncated"] = bool(merged.get("truncated")) or bool(incoming.get("truncated"))
    merged["quality"] = _merge_quality_entry(merged.get("quality"), incoming.get("quality"))
    return merged


def _merge_positive_ints(left: object, right: object, limit: int) -> list[int]:
    return _positive_ints(tuple(_list_values(left)) + tuple(_list_values(right)), limit)


def _merge_quality_entry(left: object, right: object) -> dict[str, object]:
    existing = _quality_entry(left)
    incoming = _quality_entry(right)
    merged: dict[str, object] = {}
    for key in ("level", "kind", "freshness", "independent_group"):
        value = existing.get(key) or incoming.get(key)
        if value:
            merged[key] = value
    return merged


def _source_entry(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "source_id": identifier(item.get("source_id"), 80),
        "requested_url_ref": _url_ref_entry(item.get("requested_url_ref")),
        "final_url_ref": _url_ref_entry(item.get("final_url_ref")),
        "host": identifier(item.get("host"), 120),
        "title_digest": content_digest(item.get("title_digest")),
        "content_hash": _content_hash_entry(item.get("content_hash")),
        "retrieved_at": clip(item.get("retrieved_at"), 80),
        "content_kind": identifier(item.get("content_kind"), 40) or "html",
        "page_count": nonnegative_int(item.get("page_count")),
        "pages_read": _positive_ints(item.get("pages_read"), 48),
        "truncated": bool(item.get("truncated")),
        "quality": _quality_entry(item.get("quality")),
    }


def _evidence_entry(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "evidence_id": identifier(item.get("evidence_id"), 80),
        "source_id": identifier(item.get("source_id"), 80),
        "excerpt_digest": content_digest(item.get("excerpt_digest")),
        "bounded_excerpt": clip(item.get("bounded_excerpt"), 360),
        "locator": _locator_entry(_mapping(item.get("locator"))),
        "stance": identifier(item.get("stance"), 40) or "supports",
        "note_id": identifier(item.get("note_id"), 120),
        "claim_text_digest": content_digest(item.get("claim_text_digest")),
    }


def _claim_entry(item: Mapping[str, object]) -> dict[str, object]:
    claim_text = str(item.get("claim_text") or "")
    return {
        "claim_id": identifier(item.get("claim_id"), 80),
        "claim_text_digest": digest_text(claim_text),
        "claim_chars": len(claim_text),
        "claim_section": identifier(item.get("claim_section"), 80),
        "citation_numbers": _positive_ints(item.get("citation_numbers"), 24),
        "evidence_refs": list(bounded_refs(_list_values(item.get("evidence_refs")), limit=48)),
        "assumption_refs": list(bounded_refs(_list_values(item.get("assumption_refs")), limit=48)),
        "status": identifier(item.get("status"), 40) or "unsupported",
    }


def _assumption_entry(item: Mapping[str, object]) -> dict[str, object]:
    text = str(item.get("assumption_text") or "")
    return {
        "assumption_id": identifier(item.get("assumption_id"), 80),
        "assumption_text_digest": digest_text(text),
        "assumption_chars": len(text),
        "reason": identifier(item.get("reason"), 80),
        "claim_ref": identifier(item.get("claim_ref"), 80),
    }


def _relation_entry(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "relation_id": identifier(item.get("relation_id"), 80),
        "relation_kind": identifier(item.get("relation_kind"), 40),
        "from_ref": identifier(item.get("from_ref"), 80),
        "to_ref": identifier(item.get("to_ref"), 80),
        "citation_numbers": _positive_ints(item.get("citation_numbers"), 24),
    }


def _locator_entry(item: Mapping[str, object]) -> dict[str, object]:
    locator_hash = stable_ref(
        "locator_span",
        item.get("source_id"),
        item.get("page"),
        item.get("char_start"),
        item.get("char_end"),
        item.get("locator"),
    )
    return {
        "locator_id": stable_ref(
            "locator",
            item.get("kind"),
            item.get("source_id"),
            item.get("page"),
            item.get("char_start"),
            item.get("char_end"),
            item.get("locator"),
        ),
        "kind": identifier(item.get("kind"), 40) or "unknown",
        "source_id": identifier(item.get("source_id"), 80),
        "page": nonnegative_int(item.get("page")) or None,
        "char_start": nonnegative_int(item.get("char_start")),
        "char_end": nonnegative_int(item.get("char_end")),
        "locator": clip(item.get("locator"), 80),
        "locator_hash": locator_hash,
    }


def _valid_ledger_payload(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == EVIDENCE_LEDGER_SCHEMA_VERSION
        and payload.get("kind") == EVIDENCE_LEDGER_KIND
        and isinstance(payload.get("records"), list)
        and isinstance(payload.get("sources"), dict)
        and isinstance(payload.get("evidence"), dict)
        and isinstance(payload.get("claims"), dict)
        and isinstance(payload.get("assumptions"), dict)
        and isinstance(payload.get("relations"), dict)
    )


def _canonical_ledger_payload(payload: object) -> bool:
    if not _valid_ledger_payload(payload):
        return False
    assert isinstance(payload, dict)
    if set(payload) - _TOP_LEVEL_KEYS:
        return False
    if not _stable_ref_schema_ok("evidence_ledger", payload.get("ledger_ref")):
        return False
    if not _digest_schema_ok(payload.get("session_ref")):
        return False
    if not _project_ref_schema_ok(payload.get("project_ref")):
        return False
    if not _clip_schema_ok(payload.get("updated_at"), 80, allow_empty=False):
        return False
    if not _warnings_schema_ok(payload.get("warnings", [])):
        return False
    records = _records(payload)
    if len(records) != len(payload.get("records", ())) or len(records) > MAX_LEDGER_RECORDS:
        return False
    live = {
        "sources": set(),
        "evidence": set(),
        "claims": set(),
        "assumptions": set(),
        "relations": set(),
    }
    for record in records:
        if not _record_schema_ok(record):
            return False
        refs = _record_refs(record)
        for key in live:
            live[key].update(refs[key])
    maps = {
        "sources": _mapping(payload.get("sources")),
        "evidence": _mapping(payload.get("evidence")),
        "claims": _mapping(payload.get("claims")),
        "assumptions": _mapping(payload.get("assumptions")),
        "relations": _mapping(payload.get("relations")),
    }
    if not _map_schema_ok(maps["sources"], live["sources"], "source_id", _SOURCE_KEYS):
        return False
    if not _map_schema_ok(maps["evidence"], live["evidence"], "evidence_id", _EVIDENCE_KEYS):
        return False
    if not _map_schema_ok(maps["claims"], live["claims"], "claim_id", _CLAIM_KEYS):
        return False
    if not _map_schema_ok(
        maps["assumptions"],
        live["assumptions"],
        "assumption_id",
        _ASSUMPTION_KEYS,
    ):
        return False
    if not _map_schema_ok(maps["relations"], live["relations"], "relation_id", _RELATION_KEYS):
        return False
    for source in maps["sources"].values():
        assert isinstance(source, dict)
        if (
            not _source_schema_ok(source)
            or not _url_ref_schema_ok(source.get("requested_url_ref"))
            or not _url_ref_schema_ok(source.get("final_url_ref"))
            or not _quality_schema_ok(source.get("quality"))
        ):
            return False
    for evidence in maps["evidence"].values():
        assert isinstance(evidence, dict)
        if not _evidence_schema_ok(evidence) or not _locator_schema_ok(evidence.get("locator")):
            return False
    for claim in maps["claims"].values():
        assert isinstance(claim, dict)
        if not _claim_schema_ok(claim):
            return False
    for assumption in maps["assumptions"].values():
        assert isinstance(assumption, dict)
        if not _assumption_schema_ok(assumption):
            return False
    for relation in maps["relations"].values():
        assert isinstance(relation, dict)
        if not _relation_schema_ok(relation):
            return False
    return _ledger_graph_closed(payload)


def _record_schema_ok(record: Mapping[str, object]) -> bool:
    if set(record) - _RECORD_KEYS:
        return False
    if _record_id(record.get("record_id")) != record.get("record_id"):
        return False
    if not _digest_schema_ok(record.get("record_digest")):
        return False
    if not _identifier_schema_ok(record.get("run_id"), 120):
        return False
    if not _digest_schema_ok(record.get("session_ref")):
        return False
    if not _project_ref_schema_ok(record.get("project_ref")):
        return False
    if record.get("answer_status") not in ANSWER_STATUSES:
        return False
    if not _counts_schema_ok(record.get("counts")):
        return False
    if not _record_counts_match(record):
        return False
    if not all(isinstance(record.get(key), list) for key in _REF_LIST_KEYS):
        return False
    for key in _REF_LIST_KEYS:
        if list(bounded_refs(_list_values(record.get(key)), limit=_record_ref_limit(key))) != record.get(key):
            return False
    return (
        _identifier_schema_ok(record.get("synthesis_id"), 120)
        and _identifier_schema_ok(record.get("stop_reason"), 80)
        and _clip_schema_ok(record.get("captured_at"), 80, allow_empty=False)
        and _digest_schema_ok(record.get("record_integrity"))
    )


def _map_schema_ok(
    rows: Mapping[str, object],
    live_refs: set[str],
    id_field: str,
    allowed_keys: frozenset[str],
) -> bool:
    if set(rows) != live_refs:
        return False
    for item_id, item in rows.items():
        if not isinstance(item, dict):
            return False
        if set(item) - allowed_keys:
            return False
        if identifier(item.get(id_field), 80) != item_id:
            return False
    return True


def _source_schema_ok(source: Mapping[str, object]) -> bool:
    return (
        _identifier_schema_ok(source.get("source_id"), 80, allow_empty=False)
        and _host_schema_ok(source.get("host"))
        and _optional_digest_schema_ok(source.get("title_digest"))
        and _content_hash_schema_ok(source.get("content_hash"))
        and _clip_schema_ok(source.get("retrieved_at"), 80)
        and _identifier_schema_ok(source.get("content_kind"), 40, allow_empty=False)
        and isinstance(source.get("page_count"), int)
        and int(source.get("page_count") or 0) >= 0
        and _positive_ints(source.get("pages_read"), 48) == source.get("pages_read")
        and isinstance(source.get("truncated"), bool)
    )


def _evidence_schema_ok(evidence: Mapping[str, object]) -> bool:
    return (
        _identifier_schema_ok(evidence.get("evidence_id"), 80, allow_empty=False)
        and _identifier_schema_ok(evidence.get("source_id"), 80, allow_empty=False)
        and _digest_schema_ok(evidence.get("excerpt_digest"))
        and _bounded_text_schema_ok(evidence.get("bounded_excerpt"), 360, allow_empty=False)
        and evidence.get("stance") in {"supports", "contradicts", "context", "unknown"}
        and _identifier_schema_ok(evidence.get("note_id"), 120)
        and _digest_schema_ok(evidence.get("claim_text_digest"))
    )


def _claim_schema_ok(claim: Mapping[str, object]) -> bool:
    return (
        _identifier_schema_ok(claim.get("claim_id"), 80, allow_empty=False)
        and _digest_schema_ok(claim.get("claim_text_digest"))
        and isinstance(claim.get("claim_chars"), int)
        and 0 <= int(claim.get("claim_chars") or 0) <= MAX_CLAIM_TEXT_CHARS
        and _identifier_schema_ok(claim.get("claim_section"), 80, allow_empty=False)
        and _positive_ints(claim.get("citation_numbers"), 24) == claim.get("citation_numbers")
        and list(bounded_refs(_list_values(claim.get("evidence_refs")), limit=48)) == claim.get("evidence_refs")
        and list(bounded_refs(_list_values(claim.get("assumption_refs")), limit=48))
        == claim.get("assumption_refs")
        and claim.get("status") in CLAIM_STATUSES
    )


def _assumption_schema_ok(assumption: Mapping[str, object]) -> bool:
    return (
        _identifier_schema_ok(assumption.get("assumption_id"), 80, allow_empty=False)
        and _digest_schema_ok(assumption.get("assumption_text_digest"))
        and isinstance(assumption.get("assumption_chars"), int)
        and 0 <= int(assumption.get("assumption_chars") or 0) <= MAX_CLAIM_TEXT_CHARS
        and _identifier_schema_ok(assumption.get("reason"), 80)
        and _identifier_schema_ok(assumption.get("claim_ref"), 80)
    )


def _relation_schema_ok(relation: Mapping[str, object]) -> bool:
    return (
        _identifier_schema_ok(relation.get("relation_id"), 80, allow_empty=False)
        and relation.get("relation_kind") in CLAIM_RELATION_KINDS
        and _identifier_schema_ok(relation.get("from_ref"), 80, allow_empty=False)
        and _identifier_schema_ok(relation.get("to_ref"), 80, allow_empty=False)
        and _positive_ints(relation.get("citation_numbers"), 24) == relation.get("citation_numbers")
    )


def _url_ref_schema_ok(value: object) -> bool:
    if not isinstance(value, dict):
        return value in ({}, None)
    if set(value) - _URL_REF_KEYS:
        return False
    if value.get("url_digest") and not _digest_schema_ok(value.get("url_digest")):
        return False
    if value.get("path_digest") and not _digest_schema_ok(value.get("path_digest")):
        return False
    if value.get("host") and not _host_schema_ok(value.get("host")):
        return False
    if value.get("scheme") and value.get("scheme") not in {"http", "https", "file"}:
        return False
    if "redacted" in value and not isinstance(value.get("redacted"), bool):
        return False
    return True


def _project_ref_schema_ok(value: object) -> bool:
    if not isinstance(value, dict):
        return value in ({}, None)
    if set(value) - _PROJECT_REF_KEYS:
        return False
    if value.get("digest") and not _digest_schema_ok(value.get("digest")):
        return False
    basename = str(value.get("basename") or "")
    return basename == clip(basename, 80) and "/" not in basename and "\\" not in basename


def _quality_schema_ok(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - _QUALITY_KEYS:
        return False
    return all(_identifier_schema_ok(item, 80) for item in value.values())


def _locator_schema_ok(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - _LOCATOR_KEYS:
        return False
    return (
        _stable_ref_schema_ok("locator", value.get("locator_id"))
        and _stable_ref_schema_ok("locator_span", value.get("locator_hash"))
        and _identifier_schema_ok(value.get("kind"), 40, allow_empty=False)
        and _identifier_schema_ok(value.get("source_id"), 80, allow_empty=False)
        and (value.get("page") is None or (isinstance(value.get("page"), int) and int(value["page"]) > 0))
        and isinstance(value.get("char_start"), int)
        and int(value.get("char_start") or 0) >= 0
        and isinstance(value.get("char_end"), int)
        and int(value.get("char_end") or 0) >= 0
        and _clip_schema_ok(value.get("locator"), 80)
    )


def _counts_schema_ok(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - _COUNT_KEYS:
        return False
    return all(isinstance(item, int) and item >= 0 for item in value.values())


def _record_counts_match(record: Mapping[str, object]) -> bool:
    counts = _mapping(record.get("counts"))
    expected = {
        "sources": len(_list_values(record.get("source_refs"))),
        "evidence": len(_list_values(record.get("evidence_refs"))),
        "claims": len(_list_values(record.get("claim_refs"))),
        "assumptions": len(_list_values(record.get("assumption_refs"))),
        "relations": len(_list_values(record.get("relation_refs"))),
    }
    if any(counts.get(key) != value for key, value in expected.items()):
        return False
    unsupported = counts.get("unsupported_claims")
    return isinstance(unsupported, int) and 0 <= unsupported <= expected["claims"]


def _warnings_schema_ok(value: object) -> bool:
    if not isinstance(value, list) or len(value) > MAX_WARNINGS:
        return False
    return all(item == "records_pruned_for_ledger_closure" for item in value)


def _digest_schema_ok(value: object) -> bool:
    text = str(value or "")
    suffix = text.removeprefix("sha256:")
    return (
        text.startswith("sha256:")
        and len(suffix) == 64
        and all(char in "0123456789abcdef" for char in suffix)
    )


def _content_hash_entry(value: object) -> str:
    text = str(value or "").strip()
    if _short_hex_schema_ok(text):
        return text.lower()
    if _digest_schema_ok(text):
        return text.lower()
    return ""


def _content_hash_schema_ok(value: object) -> bool:
    if value in ("", None):
        return True
    return isinstance(value, str) and (_short_hex_schema_ok(value) or _digest_schema_ok(value))


def _short_hex_schema_ok(value: object) -> bool:
    text = str(value or "")
    return len(text) == 16 and all(char in "0123456789abcdef" for char in text)


def _optional_digest_schema_ok(value: object) -> bool:
    return value in ("", None) or _digest_schema_ok(value)


def _stable_ref_schema_ok(prefix: str, value: object) -> bool:
    text = str(value or "")
    expected_prefix = f"{identifier(prefix, 40)}:"
    suffix = text.removeprefix(expected_prefix)
    return (
        text.startswith(expected_prefix)
        and len(suffix) == 16
        and all(char in "0123456789abcdef" for char in suffix)
    )


def _identifier_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return value == identifier(value, limit)


def _clip_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return value == clip(value, limit)


def _bounded_text_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    return _clip_schema_ok(value, limit, allow_empty=allow_empty)


def _host_schema_ok(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return True
    return (
        value == identifier(value, 120)
        and value == value.lower()
        and "/" not in value
        and "\\" not in value
        and "@" not in value
    )


def _record_ref_limit(key: str) -> int:
    return {
        "source_refs": MAX_LEDGER_SOURCES,
        "evidence_refs": MAX_LEDGER_EVIDENCE,
        "claim_refs": MAX_LEDGER_CLAIMS,
        "assumption_refs": MAX_LEDGER_ASSUMPTIONS,
        "relation_refs": MAX_LEDGER_RELATIONS,
    }[key]


def _ledger_graph_closed(payload: object) -> bool:
    if not _valid_ledger_payload(payload):
        return False
    assert isinstance(payload, dict)
    limits = {
        "sources": MAX_LEDGER_SOURCES,
        "evidence": MAX_LEDGER_EVIDENCE,
        "claims": MAX_LEDGER_CLAIMS,
        "assumptions": MAX_LEDGER_ASSUMPTIONS,
        "relations": MAX_LEDGER_RELATIONS,
    }
    records_value = payload.get("records")
    if not isinstance(records_value, list):
        return False
    records = _records(payload)
    if len(records) != len(records_value) or len(records) > MAX_LEDGER_RECORDS:
        return False
    maps = {key: _mapping(payload.get(key)) for key in limits}
    for key, limit in limits.items():
        if len(maps[key]) > limit:
            return False
        if any(not isinstance(value, dict) for value in maps[key].values()):
            return False
    for record in records:
        if not _record_ref_lists_within_caps(record):
            return False
        refs = _record_refs(record)
        if not _record_refs_available(refs, maps):
            return False
    return True


def _trim_maps(payload: dict[str, object]) -> None:
    limits = {
        "sources": MAX_LEDGER_SOURCES,
        "evidence": MAX_LEDGER_EVIDENCE,
        "claims": MAX_LEDGER_CLAIMS,
        "assumptions": MAX_LEDGER_ASSUMPTIONS,
        "relations": MAX_LEDGER_RELATIONS,
    }
    maps = {key: _dict_map(payload, key) for key in limits}
    records = _records(payload)[-MAX_LEDGER_RECORDS:]
    kept_reversed: list[dict[str, object]] = []
    live: dict[str, set[str]] = {key: set() for key in limits}
    pruned = len(_records(payload)) - len(records)
    for record in reversed(records):
        refs = _record_refs(record)
        if not _record_refs_available(refs, maps):
            pruned += 1
            continue
        if any(len(live[key] | refs[key]) > limits[key] for key in limits):
            pruned += 1
            continue
        kept_reversed.append(record)
        for key in limits:
            live[key].update(refs[key])
    payload["records"] = list(reversed(kept_reversed))
    for key, limit in limits.items():
        payload[key] = {
            item_id: value
            for item_id, value in maps[key].items()
            if item_id in live[key]
        }
        if len(payload[key]) > limit:
            payload[key] = dict(list(payload[key].items())[:limit])
    if pruned:
        _add_warning(payload, "records_pruned_for_ledger_closure")


def _counts(payload: Mapping[str, object]) -> dict[str, int]:
    return {
        "records": len(_records(payload)),
        "sources": len(_mapping(payload.get("sources"))),
        "evidence": len(_mapping(payload.get("evidence"))),
        "claims": len(_mapping(payload.get("claims"))),
        "assumptions": len(_mapping(payload.get("assumptions"))),
        "relations": len(_mapping(payload.get("relations"))),
    }


def _has_record(payload: Mapping[str, object], record_id: str, record_digest: str) -> bool:
    return any(
        item.get("record_id") == record_id and item.get("record_digest") == content_digest(record_digest)
        for item in _records(payload)
    )


def _records(payload: Mapping[str, object]) -> list[dict[str, object]]:
    return [item for item in payload.get("records", ()) if isinstance(item, dict)]


def _dict_map(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if isinstance(value, dict):
        rows = dict(value)
        payload[key] = rows
        return rows
    rows: dict[str, object] = {}
    payload[key] = rows
    return rows


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _url_ref_entry(value: object) -> dict[str, object]:
    raw = _mapping(value)
    if not raw:
        return {}
    payload: dict[str, object] = {}
    if raw.get("url_digest"):
        payload["url_digest"] = content_digest(raw.get("url_digest"))
    if raw.get("path_digest"):
        payload["path_digest"] = content_digest(raw.get("path_digest"))
    if raw.get("host"):
        payload["host"] = identifier(raw.get("host"), 120)
    if raw.get("scheme"):
        payload["scheme"] = identifier(raw.get("scheme"), 20)
    if raw.get("redacted"):
        payload["redacted"] = True
    return payload


def _project_ref_entry(value: object) -> dict[str, object]:
    raw = _mapping(value)
    if not raw:
        return {}
    payload: dict[str, object] = {}
    basename = str(raw.get("basename") or "").strip()
    if basename:
        payload["basename"] = clip(Path(basename).name or basename, 80)
    if raw.get("digest"):
        payload["digest"] = content_digest(raw.get("digest"))
    return payload


def _quality_entry(value: object) -> dict[str, object]:
    raw = _mapping(value)
    if not raw:
        return {}
    payload: dict[str, object] = {}
    for key in ("level", "kind", "freshness", "independent_group"):
        text = identifier(raw.get(key), 80)
        if text:
            payload[key] = text
    return payload


def _list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _list_values(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _positive_ints(value: object, limit: int) -> list[int]:
    rows: list[int] = []
    if not isinstance(value, (list, tuple)):
        return rows
    for item in value:
        number = nonnegative_int(item)
        if number > 0 and number not in rows:
            rows.append(number)
        if len(rows) >= limit:
            break
    return rows


def _record_id(value: object) -> str:
    text = identifier(value, 80)
    prefix = "research_record:"
    suffix = text.removeprefix(prefix)
    if text.startswith(prefix) and len(suffix) == 16 and all(char in "0123456789abcdef" for char in suffix):
        return text
    return ""


def _record_refs(record: Mapping[str, object]) -> dict[str, set[str]]:
    return {
        "sources": set(bounded_refs(_list_values(record.get("source_refs")), limit=MAX_LEDGER_SOURCES)),
        "evidence": set(bounded_refs(_list_values(record.get("evidence_refs")), limit=MAX_LEDGER_EVIDENCE)),
        "claims": set(bounded_refs(_list_values(record.get("claim_refs")), limit=MAX_LEDGER_CLAIMS)),
        "assumptions": set(bounded_refs(
            _list_values(record.get("assumption_refs")),
            limit=MAX_LEDGER_ASSUMPTIONS,
        )),
        "relations": set(bounded_refs(_list_values(record.get("relation_refs")), limit=MAX_LEDGER_RELATIONS)),
    }


def _record_ref_lists_within_caps(record: Mapping[str, object]) -> bool:
    return (
        len(_list_values(record.get("source_refs"))) <= MAX_LEDGER_SOURCES
        and len(_list_values(record.get("evidence_refs"))) <= MAX_LEDGER_EVIDENCE
        and len(_list_values(record.get("claim_refs"))) <= MAX_LEDGER_CLAIMS
        and len(_list_values(record.get("assumption_refs"))) <= MAX_LEDGER_ASSUMPTIONS
        and len(_list_values(record.get("relation_refs"))) <= MAX_LEDGER_RELATIONS
    )


def _record_refs_available(
    refs: Mapping[str, set[str]],
    maps: Mapping[str, Mapping[str, object]],
) -> bool:
    if any(refs[key] - set(maps[key]) for key in refs):
        return False
    for evidence_id in refs["evidence"]:
        evidence = maps["evidence"].get(evidence_id)
        if not isinstance(evidence, dict):
            return False
        evidence_source = identifier(evidence.get("source_id"), 80)
        if evidence_source not in refs["sources"]:
            return False
        locator = evidence.get("locator")
        locator_source = (
            identifier(locator.get("source_id"), 80)
            if isinstance(locator, dict)
            else ""
        )
        if locator_source and locator_source != evidence_source:
            return False
    for claim_id in refs["claims"]:
        claim = maps["claims"].get(claim_id)
        if not isinstance(claim, dict):
            return False
        evidence_refs = set(bounded_refs(_list_values(claim.get("evidence_refs")), limit=48))
        assumption_refs = set(bounded_refs(_list_values(claim.get("assumption_refs")), limit=48))
        if evidence_refs - refs["evidence"]:
            return False
        if assumption_refs - refs["assumptions"]:
            return False
    for assumption_id in refs["assumptions"]:
        assumption = maps["assumptions"].get(assumption_id)
        if not isinstance(assumption, dict):
            return False
        claim_ref = identifier(assumption.get("claim_ref"), 80)
        if claim_ref and claim_ref not in refs["claims"]:
            return False
    for relation_id in refs["relations"]:
        relation = maps["relations"].get(relation_id)
        if not isinstance(relation, dict):
            return False
        from_ref = identifier(relation.get("from_ref"), 80)
        to_ref = identifier(relation.get("to_ref"), 80)
        if from_ref not in refs["claims"]:
            return False
        if to_ref not in refs["evidence"] and to_ref not in refs["assumptions"]:
            return False
    return True


def _add_warning(payload: dict[str, object], warning: str) -> None:
    rows = [identifier(item, 120) for item in _list_values(payload.get("warnings"))]
    cleaned = identifier(warning, 120)
    if cleaned and cleaned not in rows:
        rows.append(cleaned)
    payload["warnings"] = rows[-MAX_WARNINGS:]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "EVIDENCE_LEDGER_KIND",
    "EVIDENCE_LEDGER_SCHEMA_VERSION",
    "EvidenceLedgerSnapshot",
    "EvidenceLedgerStore",
    "EvidenceLedgerWriteResult",
    "MAX_EVIDENCE_LEDGER_BYTES",
]
