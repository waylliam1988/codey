"""Replay archived Research final answers through the current done finalizer.

This is a manual-only, no-network projection. It reads existing manual result
rows plus archived prompt/reply transcript files, reconstructs a minimal ledger
from the bounded ResearchRecord payload, and runs the current
``finalize_done_answer(..., enforce_claim_support=True)`` over the archived
final ``done.answer``.

The output intentionally omits raw prompts, replies, source bodies, report
text, and evidence excerpts. It is a traffic-saving preflight before live A/B,
not release evidence by itself.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.research.done_finalizer import finalize_done_answer
from codey.research.identity import sanitize_research_url_ref
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.proof_quality import ResearchProofReview, review_research_proof
from codey.research.report_quality import parse_citation_rows, review_report_quality
from codey.research.source_document import SourceDocument
from codey.reviews.report_sections import parse_sections
from tests.manual import source_connector_ab as connector


PROBE = "research_finalizer_replay_projection"
TARGET_GAPS = (
    "claim_missing_citation",
    "claim_missing_evidence_ref",
    "claim_missing_support_relation",
    "claim_not_evidence_backed",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay archived Research done answers through the current finalizer"
    )
    parser.add_argument("--input", action="append", help="manual result JSON path or glob")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return 0
    if args.output is None:
        parser.error("--output is required unless --self-test is used")
    inputs = _expand_inputs(args.input or [])
    rows: list[dict[str, Any]] = []
    for path in inputs:
        rows.extend(_rows_for_file(path))
    payload = _summarize(rows, inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _expand_inputs(patterns: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matched = [Path(item) for item in glob.glob(pattern)]
        paths.extend(matched or [Path(pattern)])
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if path.name.endswith("-manifest.json"):
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return sorted(unique, key=lambda item: str(item))


def _rows_for_file(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [{
            "source_file": path.name,
            "replayable": False,
            "error": f"input_read_failed:{type(exc).__name__}",
        }]
    rows = payload.get("rows") or payload.get("results") or []
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if isinstance(row, Mapping):
            output.append(_project_row(dict(row), source_file=path, row_index=index))
    return output


def _project_row(row: Mapping[str, Any], *, source_file: Path, row_index: int) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_file": source_file.name,
        "row_index": row_index,
        "provider": _text(row.get("provider")),
        "case": _text(row.get("case")),
        "arm": _text(row.get("arm")),
        "replayable": False,
    }
    record = row.get("research_record")
    if not isinstance(record, Mapping):
        return {**base, "error": "missing_research_record"}
    question = _case_question(row)
    before = review_research_proof(record, question=question)
    answer = _archived_done_answer(row)
    if not answer:
        return {**base, "proof_before": _proof_summary(before), "error": "missing_archived_done_answer"}
    ledger_result = _reconstruct_ledger(record, answer)
    if ledger_result.get("error"):
        return {
            **base,
            "proof_before": _proof_summary(before),
            "error": ledger_result["error"],
            "matched_source_count": ledger_result.get("matched_source_count", 0),
        }
    ledger = ledger_result["ledger"]
    assert isinstance(ledger, ResearchLedger)
    finalized = finalize_done_answer(
        answer,
        ledger,
        question=question,
        enforce_claim_support=True,
    )
    quality = review_report_quality(
        finalized.text,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls=ledger.final_url_set(),
    )
    after_record = build_research_record(
        question=question,
        summary=finalized.text,
        ledger=ledger,
        review=quality,
        stop_reason="done",
    )
    after = review_research_proof(after_record, question=question)
    before_gaps = _target_gap_counts(before)
    after_gaps = _target_gap_counts(after)
    return {
        **base,
        "replayable": True,
        "finalizer_changed": bool(finalized.changed),
        "finalizer_reason": finalized.reason,
        "finalizer_source_count": finalized.source_count,
        "quality_ok": bool(quality.ok),
        "quality_warning_count": len(quality.warnings),
        "summary_chars_before": int(row.get("summary_chars") or len(answer)),
        "summary_chars_after": len(finalized.text),
        "matched_source_count": ledger_result.get("matched_source_count", 0),
        "reconstructed_evidence_count": len(ledger.evidence_items),
        "proof_before": _proof_summary(before),
        "proof_after": _proof_summary(after),
        "target_gap_counts_before": before_gaps,
        "target_gap_counts_after": after_gaps,
        "target_gap_delta": sum(after_gaps.values()) - sum(before_gaps.values()),
        "record_claim_count_after": len(after_record.claims),
        "unsupported_claim_count_after": after_record.unsupported_claim_count,
        "support_relation_count_after": sum(
            1 for item in after_record.relations if item.relation_kind == "supports"
        ),
    }


def _archived_done_answer(row: Mapping[str, Any]) -> str:
    journal_dir = Path(_text(row.get("journal_dir")))
    if not journal_dir:
        return ""
    answer = ""
    for ref in row.get("transcript_refs") or []:
        if not isinstance(ref, Mapping) or ref.get("mode") != "archive":
            continue
        path = journal_dir / _text(ref.get("path"))
        reply = _transcript_reply(path)
        tool = _json_tool(reply)
        if _text(tool.get("tool")) != "done":
            continue
        args = tool.get("args")
        if isinstance(args, Mapping) and _text(args.get("answer")):
            answer = _text(args.get("answer"))
    return answer


def _transcript_reply(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    return _text(payload.get("reply"))


def _json_tool(text: str) -> dict[str, Any]:
    raw = _text(text).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _reconstruct_ledger(record: Mapping[str, Any], answer: str) -> dict[str, Any]:
    sections = parse_sections(answer)
    urls = [item.url for item in parse_citation_rows(sections.get("sources", ""))]
    if not urls:
        urls = re.findall(r"https?://\S+", answer)
    url_by_digest = {
        _text(sanitize_research_url_ref(url).get("url_digest")): url
        for url in urls
        if _text(sanitize_research_url_ref(url).get("url_digest"))
    }
    source_url_by_id: dict[str, str] = {}
    for source in _mappings(record.get("sources")):
        source_id = _text(source.get("source_id"))
        final_ref = source.get("final_url_ref") if isinstance(source.get("final_url_ref"), Mapping) else {}
        requested_ref = source.get("requested_url_ref") if isinstance(source.get("requested_url_ref"), Mapping) else {}
        digest = _text(final_ref.get("url_digest") or requested_ref.get("url_digest"))
        url = url_by_digest.get(digest, "")
        if source_id and url:
            source_url_by_id[source_id] = url
    if not source_url_by_id:
        return {"error": "source_url_digest_match_failed", "matched_source_count": 0}

    excerpts_by_source: dict[str, list[str]] = {}
    for evidence in _mappings(record.get("evidence")):
        source_id = _text(evidence.get("source_id"))
        excerpt = _text(evidence.get("bounded_excerpt"))
        if source_id in source_url_by_id and excerpt:
            excerpts_by_source.setdefault(source_id, []).append(excerpt)
    if not excerpts_by_source:
        return {
            "error": "missing_reconstructable_evidence",
            "matched_source_count": len(source_url_by_id),
        }

    ledger = ResearchLedger()
    for source_id, url in source_url_by_id.items():
        snippets = excerpts_by_source.get(source_id) or ["opened source text unavailable"]
        ledger.record_open_document(SourceDocument.html(
            requested_url=url,
            final_url=url,
            title="Archived source",
            text="\n\n".join(snippets),
        ))
    items: list[EvidenceItem] = []
    claim_text_by_evidence = _claim_text_by_evidence_id(record)
    for evidence in _mappings(record.get("evidence")):
        source_id = _text(evidence.get("source_id"))
        url = source_url_by_id.get(source_id, "")
        excerpt = _text(evidence.get("bounded_excerpt"))
        if not url or not excerpt:
            continue
        evidence_id = _text(evidence.get("evidence_id"))
        items.append(EvidenceItem(
            claim=claim_text_by_evidence.get(evidence_id, excerpt),
            source_url=url,
            excerpt=excerpt,
            stance=_text(evidence.get("stance")) or "supports",
            note_id=_text(evidence.get("note_id")),
        ))
    ledger.add_evidence_items(items, note_id="archived-record-replay")
    return {
        "ledger": ledger,
        "matched_source_count": len(source_url_by_id),
    }


def _proof_summary(review: ResearchProofReview) -> dict[str, Any]:
    return {
        "ok": bool(review.ok),
        "answer_status": review.answer_status,
        "answer_coverage_score": review.answer_coverage_score,
        "missing_evidence": list(review.missing_evidence),
        "target_gap_counts": _target_gap_counts(review),
    }


def _target_gap_counts(review: ResearchProofReview) -> dict[str, int]:
    counts = Counter(
        _text(item.get("reason_code"))
        for item in review.diagnostics_payload()
        if _text(item.get("reason_code")) in TARGET_GAPS
    )
    return {key: int(counts.get(key, 0)) for key in TARGET_GAPS}


def _case_question(row: Mapping[str, Any]) -> str:
    case = connector.CASES.get(_text(row.get("case")))
    return case.question if case is not None else ""


def _claim_text_by_evidence_id(record: Mapping[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for claim in _mappings(record.get("claims")):
        if _text(claim.get("status")) != "evidence_backed":
            continue
        text = _text(claim.get("claim_text"))
        if not text:
            continue
        for evidence_id in claim.get("evidence_refs") or []:
            ref = _text(evidence_id)
            if ref and ref not in mapping:
                mapping[ref] = text
    return mapping


def _summarize(rows: list[dict[str, Any]], inputs: Sequence[Path]) -> dict[str, Any]:
    replayed = [row for row in rows if row.get("replayable")]
    before_ok = sum(1 for row in replayed if (row.get("proof_before") or {}).get("ok"))
    after_ok = sum(1 for row in replayed if (row.get("proof_after") or {}).get("ok"))
    improved = sum(1 for row in replayed if int(row.get("target_gap_delta") or 0) < 0)
    return {
        "probe": PROBE,
        "manual_only": True,
        "source_file_count": len(inputs),
        "source_files": [path.name for path in inputs],
        "row_count": len(rows),
        "replayable_row_count": len(replayed),
        "finalizer_changed_count": sum(1 for row in replayed if row.get("finalizer_changed")),
        "target_gap_improved_count": improved,
        "proof_before_ok_count": before_ok,
        "proof_after_ok_count": after_ok,
        "quality_after_ok_count": sum(1 for row in replayed if row.get("quality_ok")),
        "finalizer_reasons": dict(Counter(_text(row.get("finalizer_reason")) for row in replayed)),
        "errors": dict(Counter(_text(row.get("error")) for row in rows if row.get("error"))),
        "rows": rows,
    }


def _mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text(value: object) -> str:
    return str(value or "").strip()


def _self_test() -> None:
    assert _json_tool('{"tool":"done","args":{"answer":"x"}}')["tool"] == "done"
    assert _json_tool('```json\n{"tool":"done","args":{"answer":"x"}}\n```')["tool"] == "done"


if __name__ == "__main__":
    raise SystemExit(main())
