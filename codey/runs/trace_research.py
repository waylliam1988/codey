"""Research row projections for the Trace sidecar.

Pure ``project_*`` builders moved verbatim from ``trace.py``: each maps
one untrusted input to a bounded row ``dict`` (or ``None`` when the
input is not recordable). They never touch recorder state, dedupe sets,
manifests, files, or ``checkpoint()`` — the recorder in ``trace.py``
keeps all keys, appends, caps, warnings, and flush timing.
"""

from __future__ import annotations

from collections.abc import Mapping

from codey.policies.redaction import looks_prompt_visible_secret
from codey.research.artifact_lineage import is_valid_derived_ref
from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref
from codey.research.guards import (
    generated_ref as _generated_ref,
)
from codey.research.guards import (
    safe_connector_id as _safe_connector_id,
)
from codey.research.guards import (
    valid_digest_ref,
)
from codey.research.review_finding import (
    FINDING_KINDS,
    FINDING_SEVERITIES,
    GAP_KINDS,
    SEVERITY_WARNING,
    STATUS_OPEN,
)
from codey.research.source_trust import SOURCE_CLASSES as _SOURCE_TRUST_CLASSES
from codey.research.topic_continuity import (
    TOPIC_CONTINUITY_SCHEMA_VERSION as _TOPIC_CONTINUITY_SCHEMA_VERSION,
)
from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_schema import (
    MAX_ANALYSIS_RUNS,
    MAX_BRIEF_CLAIM_ROWS,
    MAX_BRIEF_PROJECTIONS,
    MAX_BRIEF_REFS,
    MAX_CAPSULE_ARTIFACT_REFS,
    MAX_GAP_FINDING_REFS,
    MAX_REFS,
    MAX_SOURCE_TRUST_CLASSES,
    MAX_TEXT_CHARS,
    MAX_WARNINGS,
    RESEARCH_ANSWER_STATUSES,
    REVIEW_FINDING_REF_KINDS,
)
from codey.runs.trace_values import (
    _bounded_int,
    _identifier,
    _int_or_none,
    _nonnegative_int,
    _projection_codes,
    _safe_trace_code,
    _tool_instance_id,
    _trace_list_items,
    _unit_float,
)
from codey.utils.refs import digest_text
from codey.workspace.context_epoch import valid_context_epoch_ref


def _research_answer_status(value: object) -> str:
    text = _identifier(value, 40)
    return text if text in RESEARCH_ANSWER_STATUSES else "not_answered"


def _bounded_count_mapping(value: Mapping[str, object]) -> dict[str, int]:
    allowed = {
        "records",
        "sources",
        "evidence",
        "claims",
        "assumptions",
        "relations",
    }
    return {
        key: _nonnegative_int(raw)
        for key, raw in value.items()
        if isinstance(key, str) and key in allowed
    }


def _analysis_command_display(value: object) -> tuple[str, bool]:
    text = _clip(value, 500)
    if not text:
        return "", False
    if looks_prompt_visible_secret(text):
        return "", True
    return text, False


def _research_connector_error_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    connector_id = _safe_connector_id(value.get("connector_id"))
    action = _safe_trace_code(value.get("action"), 80)
    error = _safe_trace_code(value.get("error"), 120)
    count = _nonnegative_int(value.get("count")) or 1
    if not connector_id or not action or not error:
        return {}
    return {
        "connector_id": connector_id,
        "action": action,
        "error": error,
        "count": min(999, count),
    }


def project_research_record_summary(summary: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(summary, Mapping):
        return None
    record_id = _generated_ref(summary.get("record_id"), "research_record")
    digest = valid_digest_ref(summary.get("record_digest"))
    if not record_id or not digest:
        return None
    answer_status = _research_answer_status(summary.get("answer_status"))
    return {
        "record_id": record_id,
        "answer_status": answer_status,
        "source_count": _nonnegative_int(summary.get("source_count")),
        "evidence_count": _nonnegative_int(summary.get("evidence_count")),
        "claim_count": _nonnegative_int(summary.get("claim_count")),
        "assumption_count": _nonnegative_int(summary.get("assumption_count")),
        "unsupported_claim_count": _nonnegative_int(summary.get("unsupported_claim_count")),
        "record_digest": digest,
    }


def project_evidence_ledger_write(result: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(result, Mapping):
        return None
    record_id = _generated_ref(result.get("record_id"), "research_record")
    ledger_ref = _generated_ref(result.get("ledger_ref"), "evidence_ledger")
    if not record_id:
        return None
    counts = result.get("counts")
    payload: dict[str, object] = {
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("skipped")),
        "reason_code": _safe_trace_code(result.get("reason_code"), 80),
        "ledger_ref": ledger_ref,
        "record_id": record_id,
        "counts": _bounded_count_mapping(counts if isinstance(counts, Mapping) else {}),
    }
    warnings = [
        _safe_trace_code(item, 120)
        for item in _trace_list_items(result.get("warnings"))
        if _safe_trace_code(item, 120)
    ][:MAX_WARNINGS]
    if warnings:
        payload["warnings"] = warnings
    return payload


def project_research_proof_review(review: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(review, Mapping):
        return None
    proof_ref = _generated_ref(review.get("proof_ref"), "research_proof")
    if not proof_ref:
        return None
    question_digest = valid_digest_ref(review.get("question_digest"))
    payload: dict[str, object] = {
        "proof_ref": proof_ref,
        "ok": bool(review.get("ok")),
        "answers_question": bool(review.get("answers_question")),
        "answer_status": _research_answer_status(review.get("answer_status")),
        "answer_coverage_score": _unit_float(review.get("answer_coverage_score")),
        "gap_count": _nonnegative_int(review.get("gap_count")),
        "warning_count": _nonnegative_int(review.get("warning_count")),
        "planner_signal_count": _nonnegative_int(review.get("planner_signal_count")),
        "reason_codes": [
            _safe_trace_code(item, 80)
            for item in _trace_list_items(review.get("reason_codes"))
            if _safe_trace_code(item, 80)
        ][:MAX_WARNINGS],
    }
    record_id = _generated_ref(review.get("record_id"), "research_record")
    if record_id:
        payload["record_id"] = record_id
    digest = valid_digest_ref(review.get("record_digest"))
    if digest:
        payload["record_digest"] = digest
    if payload["ok"] and (not record_id or not digest):
        return None
    if question_digest:
        payload["question_digest"] = question_digest
    return payload


def project_research_plan(plan: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(plan, Mapping):
        return None
    plan_ref = _generated_ref(plan.get("plan_ref"), "research_plan")
    if not plan_ref:
        return None
    question_digest = valid_digest_ref(plan.get("question_digest"))
    proof_ref = _generated_ref(plan.get("proof_ref"), "research_proof")
    preferences: list[str] = []
    for item in _trace_list_items(plan.get("source_preferences")):
        connector_id = _safe_connector_id(item)
        if connector_id:
            preferences.append(connector_id)
        if len(preferences) >= MAX_REFS:
            break
    payload: dict[str, object] = {
        "plan_ref": plan_ref,
        "dry_run": True,
        "max_depth": _bounded_int(plan.get("max_depth"), 1, 1),
        "max_queries": _bounded_int(plan.get("max_queries"), 1, 8),
        "max_sources": _bounded_int(plan.get("max_sources"), 1, 12),
        "query_count": _bounded_int(plan.get("query_count"), 0, 8),
        "source_preferences": preferences,
        "reason_codes": [
            _safe_trace_code(item, 80)
            for item in _trace_list_items(plan.get("reason_codes"))
            if _safe_trace_code(item, 80)
        ][:MAX_WARNINGS],
        "warnings": [
            _safe_trace_code(item, 120)
            for item in _trace_list_items(plan.get("warnings"))
            if _safe_trace_code(item, 120)
        ][:MAX_WARNINGS],
    }
    if question_digest:
        payload["question_digest"] = question_digest
    if proof_ref:
        payload["proof_ref"] = proof_ref
    return payload


def project_research_pipeline_result(result: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(result, Mapping):
        return None
    return {
        "followup_applied": bool(result.get("followup_applied")),
        "followup_rounds": _bounded_int(result.get("followup_rounds"), 0, 3),
        "stop_reason": _safe_trace_code(result.get("stop_reason"), 80),
        "planner_stop_reason": _safe_trace_code(result.get("planner_stop_reason"), 80),
        "fresh_source_count": _nonnegative_int(result.get("fresh_source_count")),
        "new_evidence_count": _nonnegative_int(result.get("new_evidence_count")),
        "final_evidence_count": _nonnegative_int(result.get("final_evidence_count")),
        "attempted_fresh_source_count": _nonnegative_int(result.get("attempted_fresh_source_count")),
        "attempted_new_evidence_count": _nonnegative_int(result.get("attempted_new_evidence_count")),
    }


def project_research_connector_error(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    connector_id = _safe_connector_id(item.get("connector_id"))
    action = _safe_trace_code(item.get("action"), 80)
    error = _safe_trace_code(item.get("error"), 120)
    count = _nonnegative_int(item.get("count")) or 1
    if not connector_id or not action or not error:
        return None
    return {
        "connector_id": connector_id,
        "action": action,
        "error": error,
        "count": count,
    }


def project_research_done_compilation(result: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(result, Mapping):
        return None
    reason = _safe_trace_code(result.get("reason"), 80)
    if not reason:
        return None
    return {
        "reason": reason,
        "source_count": _bounded_int(result.get("source_count"), 0, 64),
    }


def project_analysis_run(record: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(record, Mapping):
        return None
    ref = _generated_ref(record.get("analysis_run_id"), "analysis_run")
    tool_id = _tool_instance_id(record.get("tool_id"))
    tool_name = _identifier(record.get("tool_name"), 40)
    if not ref or not tool_id or not tool_name:
        return None
    cwd_ref = record.get("cwd_ref")
    command_display, command_display_redacted = _analysis_command_display(record.get("command_display"))
    warnings = [
        _safe_trace_code(item, 120)
        for item in _trace_list_items(record.get("warnings"))
        if _safe_trace_code(item, 120)
    ][:MAX_WARNINGS]
    if command_display_redacted and "command_display_redacted" not in warnings:
        if len(warnings) >= MAX_WARNINGS:
            warnings = warnings[: MAX_WARNINGS - 1]
        warnings.append("command_display_redacted")
    return {
        "analysis_run_id": ref,
        "run_id": str(record.get("run_id") or "")[:120],
        "tool_id": tool_id,
        "tool_name": tool_name,
        "command_digest": valid_digest_ref(record.get("command_digest")),
        "command_display": command_display,
        "cwd_ref": dict(cwd_ref) if isinstance(cwd_ref, Mapping) else {},
        "exit_code": _int_or_none(record.get("exit_code")),
        "ok": bool(record.get("ok")),
        "started_at": str(record.get("started_at") or "")[:40],
        "finished_at": str(record.get("finished_at") or "")[:40],
        "duration_ms": _int_or_none(record.get("duration_ms")),
        "managed_output_handle": str(record.get("managed_output_handle") or "")[:80],
        "output_sha256": valid_digest_ref(
            f"sha256:{record.get('output_sha256')}"
            if str(record.get("output_sha256") or "")
            else ""
        ),
        "stored_truncated": bool(record.get("stored_truncated")),
        "capture_quality": _safe_trace_code(record.get("capture_quality"), 40),
        "reproduction_status": _safe_trace_code(record.get("reproduction_status"), 40),
        "environment_digest": valid_digest_ref(record.get("environment_digest")),
        "warnings": warnings[:MAX_WARNINGS],
    }


def project_artifact_ref(item: object) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    version_id = _generated_ref(item.get("version_id"), "artifact_version")
    artifact_id = _generated_ref(item.get("artifact_id"), "artifact")
    if not artifact_id or not version_id:
        return None
    derived = [
        str(ref or "")[:120]
        for ref in _trace_list_items(item.get("derived_from"))
        if is_valid_derived_ref(ref)
    ][:8]
    return {
        "artifact_id": artifact_id,
        "version_id": version_id,
        "artifact_kind": _identifier(item.get("artifact_kind"), 40),
        "sha256": valid_digest_ref(
            f"sha256:{item.get('sha256')}"
            if str(item.get("sha256") or "")
            else ""
        ),
        "size": _nonnegative_int(item.get("size")),
        "mime": str(item.get("mime") or "")[:80],
        "origin_run_id": str(item.get("origin_run_id") or "")[:120],
        "produced_by": str(item.get("produced_by") or "")[:120],
        "stored_truncated": bool(item.get("stored_truncated")),
        "derived_from": derived,
        "warnings": [
            _safe_trace_code(warning, 120)
            for warning in _trace_list_items(item.get("warnings"))
            if _safe_trace_code(warning, 120)
        ][:4],
    }


def project_reproducibility_capsule(capsule: Mapping[str, object]) -> dict[str, object] | None:
    if not isinstance(capsule, Mapping):
        return None
    ref = _generated_ref(capsule.get("capsule_id"), "capsule")
    if not ref:
        return None
    return {
        "capsule_id": ref,
        "run_id": str(capsule.get("run_id") or "")[:120],
        "analysis_run_refs": [
            analysis_ref
            for analysis_ref in (
                _generated_ref(item, "analysis_run")
                for item in _trace_list_items(capsule.get("analysis_run_refs"))
            )
            if analysis_ref
        ][:MAX_ANALYSIS_RUNS],
        "artifact_refs": [
            version_ref
            for version_ref in (
                _generated_ref(item, "artifact_version")
                for item in _trace_list_items(capsule.get("artifact_refs"))
            )
            if version_ref
        ][:MAX_CAPSULE_ARTIFACT_REFS],
        "environment_digest": valid_digest_ref(capsule.get("environment_digest")),
        "reproduction_status": _safe_trace_code(
            capsule.get("reproduction_status"), 40
        ),
        "warnings": [
            _safe_trace_code(item, 120)
            for item in _trace_list_items(capsule.get("warnings"))
            if _safe_trace_code(item, 120)
        ][:MAX_WARNINGS],
    }


def project_review_finding(item: object) -> dict[str, object] | None:
    to_payload = getattr(item, "to_payload", None)
    raw = to_payload() if callable(to_payload) else item
    if not isinstance(raw, Mapping):
        return None
    finding_id = _generated_ref(raw.get("finding_id"), "review_finding")
    kind = _safe_trace_code(raw.get("kind"), 40)
    if not finding_id or kind not in FINDING_KINDS:
        return None
    severity = _safe_trace_code(raw.get("severity"), 20)
    payload: dict[str, object] = {
        "finding_id": finding_id,
        "kind": kind,
        "severity": severity if severity in FINDING_SEVERITIES else SEVERITY_WARNING,
        "status": STATUS_OPEN,
        "target_ref": _normalize_runtime_ref(raw.get("target_ref")),
        "reason_codes": [
            code
            for code in (
                _safe_trace_code(value, 80)
                for value in _trace_list_items(raw.get("reason_codes"))
            )
            if code
        ][:MAX_WARNINGS],
    }
    for key, ref_kind in REVIEW_FINDING_REF_KINDS.items():
        ref = _normalize_runtime_ref(raw.get(key), kind=ref_kind)
        if ref:
            payload[key] = ref
    return payload


def project_planner_gap(item: object) -> dict[str, object] | None:
    to_payload = getattr(item, "to_payload", None)
    raw = to_payload() if callable(to_payload) else item
    if not isinstance(raw, Mapping):
        return None
    gap_id = _generated_ref(raw.get("gap_id"), "planner_gap")
    gap_kind = _safe_trace_code(raw.get("gap_kind"), 40)
    if not gap_id or gap_kind not in GAP_KINDS:
        return None
    return {
        "gap_id": gap_id,
        "gap_kind": gap_kind,
        "target_ref": _normalize_runtime_ref(raw.get("target_ref")),
        "reason_codes": [
            code
            for code in (
                _safe_trace_code(value, 80)
                for value in _trace_list_items(raw.get("reason_codes"))
            )
            if code
        ][:MAX_WARNINGS],
        "finding_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="review_finding")
                for value in _trace_list_items(raw.get("finding_refs"))
            )
            if ref
        ][:MAX_GAP_FINDING_REFS],
    }


def project_source_trust_row(item: object) -> dict[str, object] | None:
    to_payload = getattr(item, "to_payload", None)
    raw = to_payload() if callable(to_payload) else item
    if not isinstance(raw, Mapping):
        return None
    source_ref = _normalize_runtime_ref(raw.get("source_ref"), kind="source")
    source_class = _safe_trace_code(raw.get("source_class"), 40)
    if not source_ref or source_class not in _SOURCE_TRUST_CLASSES:
        return None
    return {
        "source_ref": source_ref,
        "source_class": source_class,
        "tier": _bounded_int(raw.get("tier"), 1, 3),
        "freshness": _safe_trace_code(raw.get("freshness"), 20) or "undated",
        "host": _clip(raw.get("host"), 120),
        "classes": [
            cls
            for cls in (
                _safe_trace_code(value, 40)
                for value in _trace_list_items(raw.get("classes"))
            )
            if cls in _SOURCE_TRUST_CLASSES
        ][:MAX_SOURCE_TRUST_CLASSES],
        "warnings": [
            code
            for code in (
                _safe_trace_code(value, 80)
                for value in _trace_list_items(raw.get("warnings"))
            )
            if code
        ][:MAX_WARNINGS],
    }


def project_research_brief_projection(
    projection: Mapping[str, object],
) -> dict[str, object] | None:
    if not isinstance(projection, Mapping):
        return None
    record_ref = _normalize_runtime_ref(projection.get("record_ref"), kind="research_record")
    digest = valid_digest_ref(projection.get("record_digest"))
    profile_id = _safe_trace_code(projection.get("profile_id"), 80)
    if not record_ref or not digest:
        return None
    answer_status = _research_answer_status(projection.get("answer_status"))
    payload: dict[str, object] = {
        "record_ref": record_ref,
        "record_digest": digest,
        "answer_status": answer_status,
        "profile_id": profile_id,
        "claim_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="claim")
                for value in _trace_list_items(projection.get("claim_refs"))
            )
            if ref
        ][:MAX_BRIEF_REFS],
        "evidence_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="evidence")
                for value in _trace_list_items(projection.get("evidence_refs"))
            )
            if ref
        ][:MAX_BRIEF_REFS],
        "assumption_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="assumption")
                for value in _trace_list_items(projection.get("assumption_refs"))
            )
            if ref
        ][:MAX_BRIEF_REFS],
        "analysis_run_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="analysis_run")
                for value in _trace_list_items(projection.get("analysis_run_refs"))
            )
            if ref
        ][:MAX_ANALYSIS_RUNS],
        "artifact_version_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="artifact_version")
                for value in _trace_list_items(projection.get("artifact_version_refs"))
            )
            if ref
        ][:MAX_CAPSULE_ARTIFACT_REFS],
        "proof_review_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="research_proof")
                for value in _trace_list_items(projection.get("proof_review_refs"))
            )
            if ref
        ][:MAX_BRIEF_PROJECTIONS],
        "planner_gap_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="planner_gap")
                for value in _trace_list_items(projection.get("planner_gap_refs"))
            )
            if ref
        ][:MAX_BRIEF_PROJECTIONS],
        "review_finding_refs": [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="review_finding")
                for value in _trace_list_items(projection.get("review_finding_refs"))
            )
            if ref
        ][:MAX_BRIEF_PROJECTIONS],
        "contract_refs": [
            code
            for code in (
                _safe_trace_code(value, 80)
                for value in _trace_list_items(projection.get("contract_refs"))
            )
            if code
        ][:MAX_WARNINGS],
        "warnings": [
            code
            for code in (
                _safe_trace_code(value, 80)
                for value in _trace_list_items(projection.get("warnings"))
            )
            if code
        ][:MAX_WARNINGS],
    }
    claim_rows: list[dict[str, object]] = []
    claims_raw = projection.get("claims")
    for row in _trace_list_items(claims_raw):
        if not isinstance(row, Mapping) or len(claim_rows) >= MAX_BRIEF_CLAIM_ROWS:
            continue
        claim_ref = _normalize_runtime_ref(row.get("claim_ref"), kind="claim")
        status = _safe_trace_code(row.get("status"), 20)
        text = str(row.get("text") or "").strip()
        if not text or status not in {"evidence_backed", "assumption", "unsupported"}:
            continue
        entry: dict[str, object] = {
            "status": status,
            "evidence_count": _nonnegative_int(row.get("evidence_count")),
            "text_digest": digest_text(_clip(text, 260)),
        }
        if claim_ref:
            entry["claim_ref"] = claim_ref
        claim_rows.append(entry)
    if not claim_rows and not payload["claim_refs"]:
        return None
    payload["claims"] = claim_rows
    counts = projection.get("counts")
    if isinstance(counts, Mapping):
        payload["counts"] = _bounded_count_mapping(counts)
    return payload


def project_research_topic_continuity(
    projection: Mapping[str, object],
    epoch_id: str,
) -> dict[str, object] | None:
    if not isinstance(projection, Mapping) or not projection.get("admitted"):
        return None
    schema_version = projection.get("schema_version")
    if type(schema_version) is not int or schema_version != _TOPIC_CONTINUITY_SCHEMA_VERSION:
        return None
    digest = valid_digest_ref(projection.get("digest"))
    if not digest:
        return None
    epoch = valid_context_epoch_ref(epoch_id)
    if not epoch:
        return None
    items: list[dict[str, object]] = []
    for row in _trace_list_items(projection.get("items")):
        if not isinstance(row, Mapping) or len(items) >= MAX_BRIEF_REFS:
            continue
        refs = [
            ref
            for ref in (
                _clip(value, MAX_TEXT_CHARS)
                for value in _trace_list_items(row.get("refs"))
            )
            if ref
        ]
        kind = _safe_trace_code(row.get("kind"), 40)
        if not refs or not kind:
            continue
        items.append({
            "refs": refs[:MAX_BRIEF_REFS],
            "kind": kind,
            "stale": bool(row.get("stale")),
            "reason_codes": [
                code
                for code in (
                    _safe_trace_code(value, 80)
                    for value in _trace_list_items(row.get("reason_codes"))
                )
                if code
            ][:MAX_WARNINGS],
        })
    candidates: list[dict[str, object]] = []
    for row in _trace_list_items(projection.get("candidates")):
        if not isinstance(row, Mapping) or len(candidates) >= MAX_BRIEF_REFS:
            continue
        candidate_id = _clip(row.get("candidate_id"), 80)
        source_refs = [
            ref
            for ref in (
                _clip(value, MAX_TEXT_CHARS)
                for value in _trace_list_items(row.get("source_refs"))
            )
            if ref
        ]
        if not candidate_id or not source_refs:
            continue
        candidates.append({
            "candidate_id": candidate_id,
            "source_refs": source_refs[:MAX_BRIEF_REFS],
            "risk_codes": [
                code
                for code in (
                    _safe_trace_code(value, 80)
                    for value in _trace_list_items(row.get("risk_codes"))
                )
                if code
            ][:MAX_WARNINGS],
        })
    return {
        "schema_version": projection["schema_version"],
        "context_source": _identifier(projection.get("context_source"), 80),
        "digest": digest,
        "epoch_id": epoch,
        "item_count": _nonnegative_int(projection.get("item_count")),
        "candidate_count": _nonnegative_int(projection.get("candidate_count")),
        "claim_ref_count": _nonnegative_int(projection.get("claim_ref_count")),
        "truncated": bool(projection.get("truncated")),
        "reason_codes": _projection_codes(projection, "reason_codes"),
        "warnings": _projection_codes(projection, "warnings"),
        "items": items,
        "candidates": candidates,
    }
