"""Completion row projections for the Trace sidecar.

Pure ``project_*`` builders moved verbatim from ``trace.py``: each maps
one untrusted input to a bounded row ``dict`` (or ``None`` when the
input is not recordable). They never touch recorder state, dedupe sets,
manifests, files, or ``checkpoint()`` — the recorder in ``trace.py``
keeps all keys, appends, caps, warnings, and flush timing. They never
judge whether a proof holds; they only project proofs that already exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from codey.completion.contract import (
    CHECK_STATUSES as _COMPLETION_CHECK_STATUSES,
)
from codey.completion.contract import (
    COMPLETION_COMPLETE_WITH_LIMITATIONS as _COMPLETION_COMPLETE_WITH_LIMITATIONS,
)
from codey.completion.contract import (
    COMPLETION_DOMAINS as _COMPLETION_TRACE_DOMAINS,
)
from codey.completion.contract import (
    COMPLETION_SATISFIED_STATUSES as _COMPLETION_SATISFIED_STATUSES,
)
from codey.completion.contract import (
    COMPLETION_STATUSES as _COMPLETION_TRACE_STATUSES,
)
from codey.completion.edit_integrity import (
    EDIT_INTEGRITY_SEVERITIES as _EDIT_INTEGRITY_SEVERITIES,
)
from codey.completion.edit_integrity import (
    EDIT_INTEGRITY_STATUSES as _EDIT_INTEGRITY_STATUSES,
)
from codey.completion.repair_context import (
    COMPLETION_REPAIR_SCHEMA_VERSION as _COMPLETION_REPAIR_SCHEMA_VERSION,
)
from codey.policies.redaction import looks_prompt_visible_secret
from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref
from codey.research.guards import (
    generated_ref as _generated_ref,
)
from codey.research.guards import (
    valid_digest_ref,
)
from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_schema import (
    MAX_ANALYSIS_RUNS,
    MAX_CAPSULE_ARTIFACT_REFS,
    MAX_COMPLETION_CHECK_ROWS,
    MAX_GAP_FINDING_REFS,
    MAX_WARNINGS,
)
from codey.runs.trace_values import (
    _identifier,
    _nonnegative_int,
    _projection_codes,
    _safe_trace_code,
    _trace_list_items,
)
from codey.workspace.context_epoch import valid_context_epoch_ref


def project_completion_repair_context(
    projection: Mapping[str, object],
    *,
    epoch_id: str,
) -> dict[str, object] | None:
    if not isinstance(projection, Mapping) or not projection.get("admitted"):
        return None
    schema_version = projection.get("schema_version")
    if type(schema_version) is not int or schema_version != _COMPLETION_REPAIR_SCHEMA_VERSION:
        return None
    digest = valid_digest_ref(projection.get("digest"))
    if not digest:
        return None
    epoch = valid_context_epoch_ref(epoch_id)
    if not epoch:
        return None
    payload: dict[str, object] = {
        "schema_version": projection["schema_version"],
        "context_source": _identifier(projection.get("context_source"), 80),
        "digest": digest,
        "epoch_id": epoch,
        "failure_class": _safe_trace_code(projection.get("failure_class"), 40),
        "detail": _identifier(projection.get("detail"), 20),
        "check_count": _nonnegative_int(projection.get("check_count")),
        "changed_file_count": _nonnegative_int(projection.get("changed_file_count")),
        "analysis_run_ref_count": _nonnegative_int(
            projection.get("analysis_run_ref_count")
        ),
        "finding_ref_count": _nonnegative_int(projection.get("finding_ref_count")),
        "summary_chars": _nonnegative_int(projection.get("summary_chars")),
        "truncated": bool(projection.get("truncated")),
        "reason_codes": _projection_codes(projection, "reason_codes"),
        "warnings": _projection_codes(projection, "warnings"),
    }
    proof_id = _generated_ref(projection.get("proof_id"), "completion_proof")
    contract_id = _generated_ref(projection.get("contract_id"), "completion_contract")
    if proof_id:
        payload["proof_id"] = proof_id
    if contract_id:
        payload["contract_id"] = contract_id
    refused = _safe_trace_code(projection.get("refused_reason"), 80)
    if refused:
        payload["refused_reason"] = refused
    return payload


def project_completion_proof(proof: Any) -> dict[str, object] | None:
    raw = proof.to_payload() if callable(getattr(proof, "to_payload", None)) else proof
    if not isinstance(raw, Mapping):
        return None
    proof_id = _generated_ref(raw.get("proof_id"), "completion_proof")
    contract_id = _generated_ref(raw.get("contract_id"), "completion_contract")
    domain = _safe_trace_code(raw.get("domain"), 20)
    status = _safe_trace_code(raw.get("status"), 40)
    if (
        not proof_id
        or not contract_id
        or domain not in _COMPLETION_TRACE_DOMAINS
        or status not in _COMPLETION_TRACE_STATUSES
    ):
        return None
    satisfied = status in _COMPLETION_SATISFIED_STATUSES
    payload: dict[str, object] = {
        "proof_id": proof_id,
        "contract_id": contract_id,
        "domain": domain,
        "status": status,
        "satisfied": satisfied,
    }
    check_rows: list[dict[str, object]] = []
    for item in _trace_list_items(raw.get("checks")):
        if not isinstance(item, Mapping) or len(check_rows) >= MAX_COMPLETION_CHECK_ROWS:
            continue
        check_id = _safe_trace_code(item.get("check_id"), 80)
        check_status = _safe_trace_code(item.get("status"), 20)
        if not check_id or check_status not in _COMPLETION_CHECK_STATUSES:
            continue
        row: dict[str, object] = {"check_id": check_id, "status": check_status}
        reason_code = _safe_trace_code(item.get("reason_code"), 120)
        if reason_code:
            row["reason_code"] = reason_code
        check_rows.append(row)
    if not check_rows:
        return None
    ref_groups: dict[str, list[str]] = {}
    for key in ("evidence_refs", "limitation_refs", "external_refs"):
        refs = [
            _safe_trace_code(value, 160)
            for value in _trace_list_items(raw.get(key))
        ]
        ref_groups[key] = [ref for ref in refs if ref][:MAX_GAP_FINDING_REFS]
    if (
        status == _COMPLETION_COMPLETE_WITH_LIMITATIONS
        and not ref_groups["limitation_refs"]
    ):
        return None
    payload["checks"] = check_rows
    blocked_reason = _safe_trace_code(raw.get("blocked_reason"), 120)
    if blocked_reason and not satisfied:
        payload["blocked_reason"] = blocked_reason
    reason_codes = [
        code
        for code in (
            _safe_trace_code(value, 80)
            for value in _trace_list_items(raw.get("reason_codes"))
        )
        if code
    ][:MAX_WARNINGS]
    if reason_codes:
        payload["reason_codes"] = reason_codes
    # subject_ref is an opaque bounded token (run:/ledger:/research:...),
    # not necessarily a runtime ref kind, so it gets code sanitation
    # instead of ref-kind validation.
    subject_ref = _safe_trace_code(raw.get("subject_ref"), 160)
    if subject_ref:
        payload["subject_ref"] = subject_ref
    payload["finding_refs"] = [
        ref
        for ref in (
            _normalize_runtime_ref(value, kind="review_finding")
            for value in _trace_list_items(raw.get("finding_refs"))
        )
        if ref
    ][:MAX_GAP_FINDING_REFS]
    payload["analysis_run_refs"] = [
        ref
        for ref in (
            _normalize_runtime_ref(value, kind="analysis_run")
            for value in _trace_list_items(raw.get("analysis_run_refs"))
        )
        if ref
    ][:MAX_ANALYSIS_RUNS]
    payload["artifact_refs"] = [
        ref
        for ref in (
            _normalize_runtime_ref(value, kind="artifact_version")
            for value in _trace_list_items(raw.get("artifact_refs"))
        )
        if ref
    ][:MAX_CAPSULE_ARTIFACT_REFS]
    payload["diagnostic_refs"] = [
        ref
        for ref in (
            _normalize_runtime_ref(value, kind="edit_integrity")
            for value in _trace_list_items(raw.get("diagnostic_refs"))
        )
        if ref
    ][:MAX_GAP_FINDING_REFS]
    payload.update(ref_groups)
    return payload


def project_edit_integrity(observation: Any) -> dict[str, object] | None:
    raw = (
        observation.to_payload()
        if callable(getattr(observation, "to_payload", None))
        else observation
    )
    if not isinstance(raw, Mapping):
        return None
    observation_ref = _generated_ref(
        raw.get("observation_ref"), "edit_integrity"
    )
    status = _safe_trace_code(raw.get("status"), 20)
    severity = _safe_trace_code(raw.get("severity"), 20)
    if (
        not observation_ref
        or status not in _EDIT_INTEGRITY_STATUSES
        or severity not in _EDIT_INTEGRITY_SEVERITIES
    ):
        return None
    payload: dict[str, object] = {
        "observation_ref": observation_ref,
        "status": status,
        "severity": severity,
    }
    run_ref = _safe_trace_code(raw.get("run_id"), 120)
    if run_ref:
        payload["run_id"] = run_ref
    reason_codes = [
        code
        for code in (
            _safe_trace_code(value, 80)
            for value in _trace_list_items(raw.get("reason_codes"))
        )
        if code
    ][:MAX_WARNINGS]
    if reason_codes:
        payload["reason_codes"] = reason_codes
    if raw.get("user_authorized_test_edit") is True:
        payload["user_authorized_test_edit"] = True
    paths = []
    for value in _trace_list_items(raw.get("affected_paths")):
        path = _clip(value, 240)
        # Project-relative changed paths are bounded audit facts, not
        # secret material; only the redaction screen may drop one.
        if path and not looks_prompt_visible_secret(path):
            paths.append(path)
    paths = paths[:MAX_GAP_FINDING_REFS]
    if paths:
        payload["affected_paths"] = paths
    verification_refs = [
        ref
        for ref in (
            _normalize_runtime_ref(value)
            for value in _trace_list_items(raw.get("verification_refs"))
        )
        if ref
    ][:MAX_GAP_FINDING_REFS]
    if verification_refs:
        payload["verification_refs"] = verification_refs
    change_refs = [
        _safe_trace_code(value, 80)
        for value in _trace_list_items(raw.get("change_refs"))
    ]
    change_refs = [ref for ref in change_refs if ref][:MAX_GAP_FINDING_REFS]
    if change_refs:
        payload["change_refs"] = change_refs
    monitor_error_ref = _safe_trace_code(raw.get("monitor_error_ref"), 80)
    if monitor_error_ref:
        payload["monitor_error_ref"] = monitor_error_ref
    return payload
