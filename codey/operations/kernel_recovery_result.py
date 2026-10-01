"""Single recovery-result protocol: payload vs verified provenance.

``ResultPayload`` (model_text/audit/presentation/canonical/truncated) may be
rebuilt from a persisted row. ``TrustedProvenance`` (revision/fingerprint)
may only be attached after verification as ``TrustedWorkspaceProof``.

All recovery entries funnel through ``build_recovered_result``; all
recovery errors funnel through ``build_recovery_error_result`` /
``build_recovery_mismatch_result`` (no scattered ``ToolResult(...)``):

- ``recovery.delivered_from_frame`` (safe replay + settled redelivery)
- ``kernel_session_recovery.restore_task_session`` (all entry adapters)
- ``kernel_recovery`` replay/delivered slots

Frame recovery is safe-replay only, except for settled redelivery rows
(``RecoveredToolOutcome.redelivered=True``): ``edit``/``run``/``shell``
replay rows raise ``RecoveryFailed`` instead of building an unprovenanced
success, while redelivery rows rebuild the verified original receipt
without re-execution. Frame sibling ``workspace_revision``/``workspace_fingerprint`` fields and raw
``workspace_identity`` payloads never become trusted; only verified
adapters (bump, durable store, in-memory side-channel copy) create proofs.
No entry derives trust from display audit or event metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.operations.kernel_errors import RecoveryFailed
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "RecoveredResultSpec",
    "build_recovered_result",
    "build_recovery_error_result",
    "build_recovery_mismatch_result",
    "sanitize_recovery_audit",
    "spec_for_recovered_row",
    "spec_from_delivered_result",
    "spec_from_frame_row",
    "spec_from_settled_redelivery_row",
    "frame_outcome_ok",
    "frame_outcome_exit_code",
    "spec_from_memory_result",
    "spec_from_persisted_record",
]


@dataclass(frozen=True)
class RecoveredResultSpec:
    """Normalized recovery input for the single builder."""

    call: ToolCall
    model_text: object = ""
    audit: object = None
    presentation: object = None
    canonical: object = None
    truncated: object = False
    # Only a verified proof may sit here; None means untrusted display only.
    trusted_workspace: Any | None = None
    # Unsafe/edit replay that must carry provenance fails when proof missing.
    require_workspace_provenance: bool = False


def sanitize_recovery_audit(audit: object) -> dict:
    """Keep display fields; drop forgeable provenance keys (strict).

    Missing/``None`` audit uses ``{}``. A present-but-non-mapping audit
    raises ``RecoveryFailed`` instead of silently becoming ``{}``.
    ``workspace_revision``/``workspace_fingerprint``/
    ``_kernel_workspace_trusted`` are never copied from recovery display.
    A verified proof re-adds the authoritative pair via
    ``attach_trusted_workspace``.
    """
    from codey.operations.kernel_provenance import _EXECUTOR_STRIPPED_AUDIT_KEYS

    if audit is None:
        return {}
    if not isinstance(audit, Mapping):
        raise RecoveryFailed(f"recovered audit must be a mapping, got {type(audit).__name__}")
    try:
        source = dict(audit)
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    for key in _EXECUTOR_STRIPPED_AUDIT_KEYS:
        source.pop(key, None)
    return source


def _strict_display_mapping(value: object, *, field: str) -> dict:
    """Strict display mapping: None/missing -> {}, non-mapping -> fail."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RecoveryFailed(f"recovered {field} must be a mapping, got {type(value).__name__}")
    try:
        return dict(value)
    except Exception as exc:
        raise RecoveryFailed(f"recovered {field} unreadable: {exc}") from exc


def _strict_model_text(value: object) -> str:
    if value is None:
        return ""
    if type(value) is not str:
        raise RecoveryFailed(
            f"recovered model_text must be a string, got {type(value).__name__}"
        )
    return value


def _strict_truncated(value: object) -> bool:
    if value is None:
        return False
    if type(value) is not bool:
        raise RecoveryFailed(
            f"recovered truncated must be a boolean, got {type(value).__name__}"
        )
    return value


def build_recovered_result(spec: RecoveredResultSpec) -> ToolResult:
    """Single constructor for every recovered result (fail-closed)."""
    from codey.operations.kernel_provenance import attach_trusted_workspace
    from codey.operations.kernel_result import build_recovered_tool_result

    if getattr(spec, "call", None) is None:
        raise RecoveryFailed("recovered row missing call")
    audit = sanitize_recovery_audit(getattr(spec, "audit", None))
    presentation = _strict_display_mapping(getattr(spec, "presentation", None), field="presentation")
    canonical = _strict_display_mapping(getattr(spec, "canonical", None), field="canonical")
    model_text = _strict_model_text(getattr(spec, "model_text", ""))
    truncated = _strict_truncated(getattr(spec, "truncated", False))
    try:
        result = build_recovered_tool_result(
            spec.call,
            model_text=model_text,
            audit=audit,
            presentation=presentation,
            canonical=canonical,
            truncated=truncated,
        )
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovered result rebuild failed: {exc}") from exc
    proof = getattr(spec, "trusted_workspace", None)
    if proof is not None:
        return attach_trusted_workspace(result, proof)
    if bool(getattr(spec, "require_workspace_provenance", False)):
        raise RecoveryFailed("recovered result lacks verified workspace provenance")
    return result


def build_recovery_error_result(call: ToolCall, message: str) -> ToolResult:
    """Single constructor for recovery-failure errors (never success-like)."""
    try:
        return ToolResult(call=call, model_text=f"ERROR: recovery failed: {message}")
    except Exception as exc:
        raise RecoveryFailed(f"recovery error rebuild failed: {exc}") from exc


def build_recovery_mismatch_result(call: ToolCall, expected: str, actual: str) -> ToolResult:
    """Single constructor for recovery-mismatch errors (never success-like)."""
    try:
        return ToolResult(
            call=call,
            model_text=(
                "ERROR: recovery mismatch for this turn slot: "
                f"expected {expected or '?'} with args {actual or '?'}; "
                "the prior settled result was for a different tool/args and must not be reused. "
                "Stop this batch and re-issue the correct call."
            ),
        )
    except Exception as exc:
        raise RecoveryFailed(f"recovery mismatch rebuild failed: {exc}") from exc


def _call_from_row_call(call: Any) -> ToolCall:
    from codey.runtime.core.models import ToolCall as _ToolCall

    try:
        return _ToolCall(
            str(getattr(call, "name", "") or ""),
            dict(getattr(call, "args", {}) or {}),
            str(getattr(call, "call_id", "") or ""),
        )
    except Exception as exc:
        raise RecoveryFailed(f"malformed recovered call: {exc}") from exc


def _row_text_fields(outcome: Any) -> tuple[Any, Any, Any, Any, Any]:
    try:
        audit_raw = getattr(outcome, "audit", None)
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    try:
        presentation_raw = getattr(outcome, "presentation", None)
    except Exception as exc:
        raise RecoveryFailed(f"recovered presentation unreadable: {exc}") from exc
    try:
        canonical_raw = getattr(outcome, "canonical", None)
    except Exception as exc:
        raise RecoveryFailed(f"recovered canonical unreadable: {exc}") from exc
    try:
        truncated = _strict_truncated(getattr(outcome, "truncated", False))
        model_text = _strict_model_text(getattr(outcome, "model_text", ""))
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovered outcome unreadable: {exc}") from exc
    audit_dict = sanitize_recovery_audit(audit_raw)
    presentation = _strict_display_mapping(presentation_raw, field="presentation")
    canonical = _strict_display_mapping(canonical_raw, field="canonical")
    return audit_dict, presentation, canonical, truncated, model_text


def frame_outcome_ok(item: Any) -> bool:
    """Read the frame outcome status only after strict type validation."""
    outcome = getattr(item, "outcome", None)
    if outcome is None:
        raise RecoveryFailed("recovered row missing outcome")
    value = getattr(outcome, "ok", None)
    if type(value) is not bool:
        raise RecoveryFailed(
            f"recovered outcome ok must be a boolean, got {type(value).__name__}"
        )
    return value


def frame_outcome_exit_code(item: Any) -> int | None:
    outcome = getattr(item, "outcome", None)
    if outcome is None:
        raise RecoveryFailed("recovered row missing outcome")
    value = getattr(outcome, "exit_code", None)
    if value is None:
        return None
    if type(value) is not int:
        raise RecoveryFailed(
            f"recovered outcome exit_code must be an integer, got {type(value).__name__}"
        )
    return value


def _strict_slot_index(value: Any, *, field: str) -> int:
    """Strict slot index: exact int, non-bool, non-negative."""
    if type(value) is not int:
        raise RecoveryFailed(f"malformed {field}: must be exact int, got {type(value).__name__}")
    if value < 0:
        raise RecoveryFailed(f"malformed {field}: must be >= 0, got {value}")
    return value


def spec_from_frame_row(item: Any) -> RecoveredResultSpec:
    """Adapter for ``RecoveredToolOutcome``-shaped rows (safe replay only).

    Frame recovery never carries trusted workspace provenance: legacy
    sibling ``workspace_revision``/``workspace_fingerprint`` fields and raw
    ``workspace_identity`` payloads are ignored (display only). Unsafe tools
    (``edit``/``run``/``shell``) raise ``RecoveryFailed`` instead of
    building an unprovenanced success.
    """
    call = getattr(item, "call", None)
    outcome = getattr(item, "outcome", None)
    if call is None or outcome is None:
        raise RecoveryFailed("recovered row missing call/outcome")
    frame_outcome_ok(item)
    frame_outcome_exit_code(item)
    try:
        _strict_slot_index(getattr(item, "turn", None), field="turn")
        _strict_slot_index(getattr(item, "tool_index", None), field="tool_index")
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"malformed turn/index: {exc}") from exc
    call_obj = _call_from_row_call(call)
    try:
        from codey.toolchain.tool_spec import tool_requires_trusted_recovery

        if tool_requires_trusted_recovery(str(getattr(call_obj, "name", "") or "")):
            raise RecoveryFailed(
                f"frame recovery forbids unsafe tool: {getattr(call_obj, 'name', '?')}"
            )
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"frame tool class unreadable: {exc}") from exc
    audit_dict, presentation, canonical, truncated, model_text = _row_text_fields(outcome)
    return RecoveredResultSpec(
        call=call_obj,
        model_text=model_text,
        audit=audit_dict,
        presentation=presentation,
        canonical=canonical,
        truncated=truncated,
        trusted_workspace=None,
    )


def spec_from_settled_redelivery_row(item: Any) -> RecoveredResultSpec:
    """Adapter for settled-result redelivery rows (any tool, no re-execution).

    Redelivery rows carry the original settled receipt (verified at recovery
    time, including the managed-output handle when present). They are never
    re-executed, so the safe-replay unsafe-tool ban does not apply. Like all
    frame rows they carry no trusted workspace provenance.
    """
    call = getattr(item, "call", None)
    outcome = getattr(item, "outcome", None)
    if call is None or outcome is None:
        raise RecoveryFailed("recovered row missing call/outcome")
    frame_outcome_ok(item)
    frame_outcome_exit_code(item)
    try:
        _strict_slot_index(getattr(item, "turn", None), field="turn")
        _strict_slot_index(getattr(item, "tool_index", None), field="tool_index")
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"malformed turn/index: {exc}") from exc
    call_obj = _call_from_row_call(call)
    if not str(getattr(call_obj, "name", "") or "").strip():
        raise RecoveryFailed("recovered row missing tool name")
    audit_dict, presentation, canonical, truncated, model_text = _row_text_fields(outcome)
    return RecoveredResultSpec(
        call=call_obj,
        model_text=model_text,
        audit=audit_dict,
        presentation=presentation,
        canonical=canonical,
        truncated=truncated,
        trusted_workspace=getattr(item, "workspace_proof", None),
    )


def spec_for_recovered_row(item: Any) -> RecoveredResultSpec:
    """Dispatch one recovered row to its single spec builder.

    Settled redelivery (``redelivered=True``) rebuilds the original receipt
    for any tool; all other rows stay on the safe-replay-only path. The
    marker must be an exact bool: truthy/falsy non-bools never select a
    path silently.
    """
    try:
        raw = getattr(item, "redelivered", False)
    except Exception as exc:
        raise RecoveryFailed(f"recovered row unreadable: {exc}") from exc
    if type(raw) is not bool:
        raise RecoveryFailed("recovered redelivered must be a boolean")
    if raw:
        return spec_from_settled_redelivery_row(item)
    return spec_from_frame_row(item)


def spec_from_memory_result(stored: ToolResult, call: ToolCall) -> RecoveredResultSpec:
    """Adapter for in-memory settled results (side-channel copy only)."""
    from codey.operations.kernel_provenance import (
        _is_trusted_identity,
        _kernel_workspace_identity_of,
        _trusted_workspace_proof,
    )

    try:
        identity = _kernel_workspace_identity_of(stored)
    except Exception as exc:
        raise RecoveryFailed(f"recovered workspace provenance unreadable: {exc}") from exc
    proof = (
        _trusted_workspace_proof(identity, "in_memory_kernel_result")
        if identity is not None and _is_trusted_identity(identity)
        else None
    )
    try:
        audit_raw = getattr(stored, "audit", None)
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    try:
        presentation_raw = getattr(stored, "presentation", None)
    except Exception as exc:
        raise RecoveryFailed(f"recovered presentation unreadable: {exc}") from exc
    try:
        canonical_raw = getattr(stored, "canonical", None)
    except Exception as exc:
        raise RecoveryFailed(f"recovered canonical unreadable: {exc}") from exc
    audit = sanitize_recovery_audit(audit_raw)
    presentation = _strict_display_mapping(presentation_raw, field="presentation")
    canonical = _strict_display_mapping(canonical_raw, field="canonical")
    try:
        truncated = _strict_truncated(getattr(stored, "truncated", False))
        model_text = _strict_model_text(getattr(stored, "model_text", ""))
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovered outcome unreadable: {exc}") from exc
    return RecoveredResultSpec(
        call=call,
        model_text=model_text,
        audit=audit,
        presentation=presentation,
        canonical=canonical,
        truncated=truncated,
        trusted_workspace=proof,
    )


def spec_from_persisted_record(
    record: Any,
    call: ToolCall,
    *,
    verified_identity: Any | None,
    require_provenance: bool = False,
) -> RecoveredResultSpec:
    """Adapter for persisted ``executed`` records (verified identity only)."""
    from codey.operations.kernel_provenance import _is_trusted_identity, _trusted_workspace_proof

    proof = None
    if verified_identity is not None and _is_trusted_identity(verified_identity):
        proof = _trusted_workspace_proof(verified_identity, "persisted_revision_store")
    try:
        excerpt = record.get("excerpt", "") if isinstance(record, Mapping) else ""
        if type(excerpt) is not str:
            raise RecoveryFailed("persisted replay excerpt must be a string")
    except Exception as exc:
        raise RecoveryFailed(f"persisted replay unreadable: {exc}") from exc
    audit: dict = {}
    try:
        name = record.get("name", "") if isinstance(record, Mapping) else ""
        if type(name) is not str:
            raise RecoveryFailed("persisted replay name must be a string")
        if name.strip().lower() == "edit":
            audit["changed"] = True
        if isinstance(record, Mapping) and "exit_code" in record:
            exit_code = record["exit_code"]
            if type(exit_code) is not int:
                raise RecoveryFailed("persisted replay exit_code must be an integer")
            audit["exit_code"] = exit_code
    except Exception as exc:
        raise RecoveryFailed(f"persisted replay unreadable: {exc}") from exc
    return RecoveredResultSpec(
        call=call,
        model_text=excerpt,
        audit=audit,
        presentation={},
        canonical={},
        truncated=False,
        trusted_workspace=proof,
        require_workspace_provenance=require_provenance,
    )


def spec_from_delivered_result(stored: ToolResult, call: ToolCall) -> RecoveredResultSpec:
    """Adapter for already-built delivered results (side-channel only)."""
    return spec_from_memory_result(stored, call)
