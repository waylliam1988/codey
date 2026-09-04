"""Manual-only projection for Research claim support gaps.

This script reads historical result JSON, full ResearchRecord payloads, or
archived transcript/report files and emits bounded proof-gap projections. It is
an experiment aid only: it never rewrites production reports and never copies
raw prompt, reply, source, or report bodies into its output.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.proof_quality import ResearchProofReview, review_research_proof
from codey.reviews.report_sections import parse_sections
from codey.utils.refs import digest_json, digest_text, identifier, stable_ref
from tests.manual.ab_harness_common import timestamp, write_json_atomic

PROBE = "research_claim_support_projection"
MAX_ITEMS = 400
MAX_CLAIM_REFS = 80
TARGET_GAP_CODES = (
    "claim_missing_citation",
    "claim_missing_evidence_ref",
    "claim_missing_support_relation",
    "claim_not_evidence_backed",
)

_CITATION_RE = re.compile(r"\[\d+(?:[^\]]*)?\]")


def expand_inputs(inputs: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in inputs:
        value = str(raw or "").strip()
        if not value:
            continue
        candidate = Path(value)
        matches = sorted(candidate.parent.glob(candidate.name)) if any(ch in value for ch in "*?[") else [candidate]
        paths.extend(matches)
    return _clean_paths(paths)


def build_projection(paths: Sequence[Path], *, question: str = "") -> dict[str, Any]:
    loaded: list[tuple[str, object]] = []
    for path in _clean_paths(paths):
        payload = _load_input(path)
        if payload is not None:
            loaded.append((path.name, payload))
    return build_projection_from_inputs(loaded, question=question)


def build_projection_from_inputs(inputs: Sequence[tuple[str, object]], *, question: str = "") -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    source_files = sorted({Path(source_file).name for source_file, _payload in inputs})
    for source_file, payload in inputs:
        items.extend(_project_payload(payload, source_file=Path(source_file).name, question=question))
        if len(items) >= MAX_ITEMS:
            break
    items = items[:MAX_ITEMS]
    return {
        "probe": PROBE,
        "manual_only": True,
        "generated_at": timestamp(),
        "source_file_count": len(source_files),
        "source_files": source_files[:120],
        "item_count": len(items),
        "record_projection_count": sum(
            1 for item in items if item.get("projection_kind") == "record_claim_support_projection"
        ),
        "row_gap_summary_count": sum(1 for item in items if item.get("projection_kind") == "row_gap_summary"),
        "report_probe_count": sum(1 for item in items if item.get("projection_kind") == "report_digest_probe"),
        "target_gap_counts": _aggregate_target_gap_counts(items),
        "projection_improved_count": sum(1 for item in items if _item_projection_improved(item)),
        "items": items,
    }


def _project_payload(payload: object, *, source_file: str, question: str) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        return [_report_probe(payload, source_file=source_file, context="text_file")]
    if isinstance(payload, list):
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(payload):
            if isinstance(item, Mapping):
                rows.extend(_project_row(item, source_file=source_file, row_index=index, probe="", question=question))
        return rows
    if not isinstance(payload, Mapping):
        return []
    record = _extract_record(payload)
    if record:
        return [_record_projection(record, source_file=source_file, row_index=None, row={}, probe="", question=question)]
    rows = payload.get("rows")
    if isinstance(rows, list):
        projected: list[dict[str, Any]] = []
        probe = _safe_text(payload.get("probe"))
        for index, row in enumerate(rows):
            if isinstance(row, Mapping):
                projected.extend(_project_row(row, source_file=source_file, row_index=index, probe=probe, question=question))
        return projected
    report = _report_text_from_payload(payload)
    if report:
        context = "archived_transcript" if _safe_text(payload.get("reply")) else "report_payload"
        return [_report_probe(report, source_file=source_file, context=context)]
    return []


def _project_row(
    row: Mapping[str, Any],
    *,
    source_file: str,
    row_index: int,
    probe: str,
    question: str,
) -> list[dict[str, Any]]:
    record = _extract_record(row)
    if record:
        return [
            _record_projection(
                record,
                source_file=source_file,
                row_index=row_index,
                row=row,
                probe=probe,
                question=question,
            )
        ]
    return [_row_gap_summary(row, source_file=source_file, row_index=row_index, probe=probe)]


def _record_projection(
    record: Mapping[str, Any],
    *,
    source_file: str,
    row_index: int | None,
    row: Mapping[str, Any],
    probe: str,
    question: str,
) -> dict[str, Any]:
    before = review_research_proof(dict(record), question=question)
    diagnostics = before.diagnostics_payload()
    claim_gaps = _claim_gap_rows(diagnostics)
    problem_refs = tuple(item["claim_ref"] for item in claim_gaps if item.get("claim_ref"))
    required_problem_refs = _required_problem_claim_refs(record, problem_refs)
    delete_payload = _project_record(record, required_problem_refs, mode="delete")
    downgrade_payload = _project_record(record, required_problem_refs, mode="downgrade")
    delete_review = review_research_proof(delete_payload, question=question)
    downgrade_review = review_research_proof(downgrade_payload, question=question)
    before_counts = _diagnostic_gap_counts(diagnostics)
    payload: dict[str, Any] = {
        "projection_kind": "record_claim_support_projection",
        "source_file": _safe_text(source_file),
        "row_index": row_index,
        "probe": _safe_text(probe),
        "provider": _safe_text(row.get("provider")),
        "case": _safe_text(row.get("case")),
        "arm": _safe_text(row.get("arm")),
        "record_id": _safe_text(record.get("record_id")),
        "record_digest": _digest_or_empty(record.get("record_digest")),
        "proof_before": _proof_summary(before),
        "target_gap_counts": before_counts,
        "target_gap_total": sum(before_counts.values()),
        "claim_gap_rows": claim_gaps[:MAX_CLAIM_REFS],
        "problem_claim_refs": list(problem_refs[:MAX_CLAIM_REFS]),
        "projected_delete": _projection_result(
            before_counts,
            delete_review,
            required_problem_refs,
            action="delete_flagged_required_claims",
        ),
        "projected_downgrade": _projection_result(
            before_counts,
            downgrade_review,
            required_problem_refs,
            action="downgrade_flagged_required_claims_to_limitations",
        ),
    }
    payload["recommendation"] = _projection_recommendation(payload)
    return payload


def _row_gap_summary(
    row: Mapping[str, Any],
    *,
    source_file: str,
    row_index: int,
    probe: str,
) -> dict[str, Any]:
    missing = _missing_evidence_from_row(row)
    counts = _reason_gap_counts(missing)
    payload: dict[str, Any] = {
        "projection_kind": "row_gap_summary",
        "source_file": _safe_text(source_file),
        "row_index": row_index,
        "probe": _safe_text(probe),
        "provider": _safe_text(row.get("provider")),
        "case": _safe_text(row.get("case")),
        "arm": _safe_text(row.get("arm")),
        "proof_ok": _bool_or_none(row.get("proof_ok")),
        "proof_answer_status": _safe_text(row.get("proof_answer_status")),
        "target_gap_counts": counts,
        "target_gap_total": sum(counts.values()),
        "can_project_claims": False,
        "reason_code": "missing_research_record",
        "proof_improvement_projection": "unknown_without_research_record",
    }
    report = _report_text_from_payload(row)
    if report:
        payload["report_probe"] = _report_probe_metrics(report)
    return payload


def _report_probe(report: str, *, source_file: str, context: str) -> dict[str, Any]:
    return {
        "projection_kind": "report_digest_probe",
        "source_file": _safe_text(source_file),
        "context": _safe_text(context),
        "can_project_claims": False,
        "reason_code": "missing_research_record",
        "proof_improvement_projection": "unknown_without_research_record",
        "report_probe": _report_probe_metrics(report),
    }


def _report_probe_metrics(report: str) -> dict[str, Any]:
    sections = parse_sections(report)
    claim_lines = _report_claim_lines(sections.get("conclusion", "")) + _report_claim_lines(sections.get("evidence", ""))
    missing_citation = [line for line in claim_lines if not _CITATION_RE.search(line)]
    source_lines = [line for line in str(sections.get("sources", "")).splitlines() if line.strip()]
    return {
        "report_digest": digest_text(report),
        "report_chars": len(str(report or "")),
        "claim_line_count_estimate": len(claim_lines),
        "claim_missing_citation_estimate": len(missing_citation),
        "citation_marker_count": len(_CITATION_RE.findall(report)),
        "source_line_count_estimate": len(source_lines),
    }


def _claim_gap_rows(diagnostics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    gaps_by_claim: dict[str, list[str]] = {}
    for diag in diagnostics:
        code = _safe_text(diag.get("reason_code"))
        if code not in TARGET_GAP_CODES:
            continue
        claim_ref = _safe_text(diag.get("claim_ref"))
        if not claim_ref:
            continue
        gaps_by_claim.setdefault(claim_ref, [])
        if code not in gaps_by_claim[claim_ref]:
            gaps_by_claim[claim_ref].append(code)
    return [
        {"claim_ref": claim_ref, "gaps": tuple(gaps)}
        for claim_ref, gaps in sorted(gaps_by_claim.items())
    ]


def _projection_result(
    before_counts: Mapping[str, int],
    review: ResearchProofReview,
    claim_refs: Sequence[str],
    *,
    action: str,
) -> dict[str, Any]:
    after_counts = _diagnostic_gap_counts(review.diagnostics_payload())
    before_total = sum(int(value) for value in before_counts.values())
    after_total = sum(after_counts.values())
    return {
        "action": action,
        "claim_ref_count": len(claim_refs),
        "claim_refs": list(claim_refs[:MAX_CLAIM_REFS]),
        "proof_after": _proof_summary(review),
        "target_gap_counts_after": after_counts,
        "target_gap_total_after": after_total,
        "target_gap_delta": after_total - before_total,
        "improves_target_gaps": after_total < before_total,
    }


def _projection_recommendation(payload: Mapping[str, Any]) -> str:
    delete = payload.get("projected_delete")
    downgrade = payload.get("projected_downgrade")
    if isinstance(delete, Mapping) and delete.get("improves_target_gaps"):
        return "delete_or_rewrite_flagged_claims"
    if isinstance(downgrade, Mapping) and downgrade.get("improves_target_gaps"):
        return "downgrade_flagged_claims_to_limitations"
    if payload.get("problem_claim_refs"):
        return "collect_more_evidence_before_promoting_claims"
    return "no_target_claim_support_projection"


def _proof_summary(review: ResearchProofReview) -> dict[str, Any]:
    payload = review.to_payload()
    return {
        "ok": bool(payload.get("ok")),
        "answer_status": _safe_text(payload.get("answer_status")),
        "answer_coverage_score": payload.get("answer_coverage_score"),
        "missing_evidence": _reason_codes(payload.get("missing_evidence")),
        "target_gap_counts": _diagnostic_gap_counts(review.diagnostics_payload()),
    }


def _project_record(record: Mapping[str, Any], claim_refs: Sequence[str], *, mode: str) -> dict[str, Any]:
    claim_ref_set = set(claim_refs)
    payload = copy.deepcopy(dict(record))
    projected_claims: list[dict[str, Any]] = []
    for claim in _list_of_mappings(payload.get("claims")):
        claim_id = _safe_text(claim.get("claim_id"))
        if claim_id in claim_ref_set and mode == "delete":
            continue
        next_claim = dict(claim)
        if claim_id in claim_ref_set and mode == "downgrade":
            next_claim["claim_section"] = "counter"
            next_claim["status"] = "assumption"
            next_claim["citation_numbers"] = []
            next_claim["evidence_refs"] = []
        projected_claims.append(next_claim)
    payload["claims"] = projected_claims
    payload["relations"] = [
        dict(relation)
        for relation in _list_of_mappings(payload.get("relations"))
        if _safe_text(relation.get("from_ref")) not in claim_ref_set
    ]
    payload["unsupported_claim_count"] = _unsupported_required_claim_count(projected_claims)
    payload["answer_status"] = _projected_answer_status(projected_claims)
    original_id = _safe_text(payload.get("record_id"))
    digest_base = copy.deepcopy(payload)
    digest_base.pop("record_digest", None)
    digest_base.pop("record_id", None)
    payload["record_digest"] = digest_json({
        "manual_claim_support_projection": mode,
        "record": digest_base,
    })
    payload["record_id"] = stable_ref("research_record", original_id, payload["record_digest"], mode)
    return payload


def _required_problem_claim_refs(record: Mapping[str, Any], problem_refs: Sequence[str]) -> tuple[str, ...]:
    problem_ref_set = set(problem_refs)
    refs: list[str] = []
    for claim in _list_of_mappings(record.get("claims")):
        claim_id = _safe_text(claim.get("claim_id"))
        if claim_id in problem_ref_set and _is_required_claim(claim):
            refs.append(claim_id)
    return tuple(refs[:MAX_CLAIM_REFS])


def _unsupported_required_claim_count(claims: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        1
        for claim in claims
        if _is_required_claim(claim) and _safe_text(claim.get("status")) != "evidence_backed"
    )


def _projected_answer_status(claims: Sequence[Mapping[str, Any]]) -> str:
    required = [claim for claim in claims if _is_required_claim(claim)]
    if not required:
        return "insufficient_evidence"
    if _unsupported_required_claim_count(required):
        return "partial"
    return "answered"


def _is_required_claim(claim: Mapping[str, Any]) -> bool:
    return _safe_text(claim.get("claim_section")) in {"conclusion", "evidence"}


def _extract_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    if _is_record_payload(payload):
        return dict(payload)
    for key in ("research_record", "record_payload", "final_record", "record"):
        value = payload.get(key)
        if isinstance(value, Mapping) and _is_record_payload(value):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping) and _is_record_payload(parsed):
                return dict(parsed)
    return {}


def _is_record_payload(payload: Mapping[str, Any]) -> bool:
    if payload.get("kind") == "research_record":
        return True
    claims = payload.get("claims")
    return bool(
        isinstance(claims, list)
        and any(isinstance(claim, Mapping) and claim.get("claim_id") for claim in claims)
        and all(isinstance(payload.get(key), list) for key in ("sources", "evidence", "relations"))
        and _safe_text(payload.get("answer_status"))
    )


def _missing_evidence_from_row(row: Mapping[str, Any]) -> tuple[str, ...]:
    value = row.get("proof_missing_evidence")
    if not isinstance(value, (list, tuple)):
        review = row.get("proof_review")
        value = review.get("missing_evidence") if isinstance(review, Mapping) else ()
    return _reason_codes(value)


def _reason_gap_counts(codes: Sequence[str]) -> dict[str, int]:
    code_set = set(codes)
    return {code: int(code in code_set) for code in TARGET_GAP_CODES}


def _diagnostic_gap_counts(diagnostics: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        code: sum(1 for diag in diagnostics if _safe_text(diag.get("reason_code")) == code)
        for code in TARGET_GAP_CODES
    }


def _aggregate_target_gap_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {code: 0 for code in TARGET_GAP_CODES}
    for item in items:
        target_counts = item.get("target_gap_counts")
        if not isinstance(target_counts, Mapping):
            continue
        for code in TARGET_GAP_CODES:
            counts[code] += max(0, _int(target_counts.get(code)))
    return counts


def _item_projection_improved(item: Mapping[str, Any]) -> bool:
    for key in ("projected_delete", "projected_downgrade"):
        value = item.get(key)
        if isinstance(value, Mapping) and value.get("improves_target_gaps"):
            return True
    return False


def _report_text_from_payload(payload: Mapping[str, Any]) -> str:
    for key in ("summary", "summary_text", "summary_preview", "report", "reply", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _report_claim_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)", "", raw).strip()
        if line:
            lines.append(line)
    return lines


def _reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    codes: list[str] = []
    for item in value:
        code = _safe_text(item)
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _list_of_mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _load_input(path: Path) -> object | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _clean_paths(paths: Iterable[Path]) -> list[Path]:
    clean: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        candidate = Path(path)
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        clean.append(candidate)
    return sorted(clean, key=lambda item: item.as_posix())


def _digest_or_empty(value: object) -> str:
    text = str(value or "").strip()
    suffix = text.removeprefix("sha256:")
    if text.startswith("sha256:") and len(suffix) == 64 and all(ch in "0123456789abcdef" for ch in suffix):
        return text
    return ""


def _bool_or_none(value: object) -> bool | None:
    return bool(value) if isinstance(value, bool) else None


def _safe_text(value: object, limit: int = 120) -> str:
    return identifier(value, limit)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _self_test_record() -> dict[str, Any]:
    source_id = "source:0000000000000001"
    evidence_id = "evidence:0000000000000002"
    good_claim_id = "claim:0000000000000003"
    bad_claim_id = "claim:0000000000000004"
    return {
        "schema_version": 1,
        "kind": "research_record",
        "record_id": "research_record:" + "a" * 16,
        "record_digest": "sha256:" + "b" * 64,
        "question": {
            "question_id": "question:" + "c" * 16,
            "question_text_digest": "sha256:" + "d" * 64,
            "chars": 22,
        },
        "answer_status": "partial",
        "sources": [{
            "source_id": source_id,
            "final_url_ref": {"url_digest": "sha256:" + "1" * 64, "host": "example.com"},
            "title_digest": "sha256:" + "2" * 64,
            "content_hash": "hash",
            "content_kind": "html",
        }],
        "evidence": [{
            "evidence_id": evidence_id,
            "source_id": source_id,
            "excerpt_digest": "sha256:" + "3" * 64,
            "bounded_excerpt": "Helium is separated from natural gas streams.",
            "locator": {"kind": "html", "source_id": source_id, "char_start": 0, "char_end": 20},
            "stance": "supports",
            "claim_text_digest": "sha256:" + "4" * 64,
        }],
        "claims": [
            {
                "claim_id": good_claim_id,
                "claim_text": "Helium supply depends on gas processing.",
                "claim_section": "conclusion",
                "citation_numbers": [1],
                "evidence_refs": [evidence_id],
                "status": "evidence_backed",
            },
            {
                "claim_id": bad_claim_id,
                "claim_text": "Raw unsupported pricing claim should never be copied.",
                "claim_section": "conclusion",
                "citation_numbers": [],
                "evidence_refs": [],
                "status": "unsupported",
            },
        ],
        "relations": [{
            "relation_id": "relation:0000000000000005",
            "relation_kind": "supports",
            "from_ref": good_claim_id,
            "to_ref": evidence_id,
            "citation_numbers": [1],
        }],
        "unsupported_claim_count": 1,
        "stop_reason": "done",
    }


def _self_test() -> None:
    payload = build_projection_from_inputs(
        [(
            "self-test.json",
            {
                "probe": "manual_self_test",
                "rows": [{
                    "provider": "fake",
                    "case": "helium",
                    "arm": "baseline",
                    "research_record": _self_test_record(),
                    "summary_text": "RAW REPORT SHOULD NOT BE COPIED",
                }],
            },
        )],
        question="Research helium supply",
    )
    assert payload["record_projection_count"] == 1
    assert payload["projection_improved_count"] == 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "Raw unsupported pricing claim" not in serialized
    assert "RAW REPORT SHOULD NOT BE COPIED" not in serialized


def main() -> int:
    parser = argparse.ArgumentParser(description="Project Research claim support gaps from manual result files")
    parser.add_argument("--input", action="append", help="result/transcript/report JSON path or glob")
    parser.add_argument("--output", type=Path, help="write projection JSON")
    parser.add_argument("--question", default="", help="optional question text for proof coverage scoring")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0
    if not args.input:
        parser.error("--input is required unless --self-test is used")
    projection = build_projection(expand_inputs(args.input), question=args.question)
    text = json.dumps(projection, ensure_ascii=False, indent=2)
    if args.output:
        write_json_atomic(args.output, projection)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
