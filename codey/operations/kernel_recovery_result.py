"""Single recovery-result protocol: payload vs verified provenance.

``ResultPayload`` (model_text/audit/presentation/canonical/truncated) may be
rebuilt from a persisted row. ``TrustedProvenance`` (revision/fingerprint)
may only be attached after verification as ``TrustedWorkspaceProof``.

All five recovery entries funnel through ``build_recovered_result``:

- ``recovery.delivered_from_frame``
- ``task_entry._entry_recovery``
- ``project_adapter._recovered_result_for_row``
- ``kernel_recovery._replay_settled_slot``
- ``kernel_recovery._delivered_slot_result``

Source adapters decide "can this be trusted"; the builder decides "how to
construct". No entry derives trust from display audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codey.operations.kernel_errors import RecoveryFailed
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "RecoveredResultSpec",
    "build_recovered_result",
    "sanitize_recovery_audit",
    "spec_from_delivered_result",
    "spec_from_frame_row",
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
    """Keep display fields; drop forgeable provenance keys.

    ``workspace_revision``/``workspace_fingerprint``/
    ``_kernel_workspace_trusted`` are never copied from recovery display.
    A verified proof re-adds the authoritative pair via
    ``attach_trusted_workspace``.
    """
    from codey.operations.kernel_provenance import _EXECUTOR_STRIPPED_AUDIT_KEYS

    try:
        source = dict(audit) if isinstance(audit, dict) else {}
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    for key in _EXECUTOR_STRIPPED_AUDIT_KEYS:
        source.pop(key, None)
    return source


def build_recovered_result(spec: RecoveredResultSpec) -> ToolResult:
    """Single constructor for every recovered result (fail-closed)."""
    from codey.operations.kernel_provenance import attach_trusted_workspace
    from codey.operations.kernel_result import build_recovered_tool_result

    if getattr(spec, "call", None) is None:
        raise RecoveryFailed("recovered row missing call")
    audit = sanitize_recovery_audit(getattr(spec, "audit", None))
    try:
        result = build_recovered_tool_result(
            spec.call,
            model_text=getattr(spec, "model_text", ""),
            audit=audit,
            presentation=getattr(spec, "presentation", None),
            canonical=getattr(spec, "canonical", None),
            truncated=getattr(spec, "truncated", False),
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
        audit = getattr(outcome, "audit", {}) or {}
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    try:
        presentation = getattr(outcome, "presentation", {}) or {}
    except Exception as exc:
        raise RecoveryFailed(f"recovered presentation unreadable: {exc}") from exc
    try:
        canonical = getattr(outcome, "canonical", {}) or {}
    except Exception as exc:
        raise RecoveryFailed(f"recovered canonical unreadable: {exc}") from exc
    try:
        truncated = bool(getattr(outcome, "truncated", False))
        model_text = str(getattr(outcome, "model_text", "") or "")
    except Exception as exc:
        raise RecoveryFailed(f"recovered outcome unreadable: {exc}") from exc
    # edit changed flag: row outcome knows it, display audit may not.
    try:
        audit_dict = dict(audit) if isinstance(audit, dict) else {}
    except Exception:
        audit_dict = {}
    return audit_dict, presentation, canonical, truncated, model_text


def spec_from_frame_row(item: Any, *, changed_fallback: bool = False) -> RecoveredResultSpec:
    """Adapter for ``RecoveredToolOutcome``-shaped rows (frame/delivery).

    Trust comes only from the kernel-owned ``workspace_identity`` payload
    carried beside audit; display audit never confers trust. When the payload
    is present but only format-valid, it still needs a durable check by the
    caller: this adapter wraps it as a proof with source
    ``frame_kernel_payload`` only when the caller has already decided the
    frame itself is kernel-owned (in-memory recovery). Persisted frames must
    verify via the store before calling the builder.
    """
    call = getattr(item, "call", None)
    outcome = getattr(item, "outcome", None)
    if call is None or outcome is None:
        raise RecoveryFailed("recovered row missing call/outcome")
    try:
        int(getattr(item, "turn", None))
        int(getattr(item, "tool_index", None))
    except Exception as exc:
        raise RecoveryFailed(f"malformed turn/index: {exc}") from exc
    call_obj = _call_from_row_call(call)
    audit_dict, presentation, canonical, truncated, model_text = _row_text_fields(outcome)
    # edit changed補完: outcome.changed is the source, not audit guess.
    try:
        if str(getattr(call, "name", "") or "") == "edit" and "changed" not in (audit_dict or {}):
            audit_dict["changed"] = bool(getattr(outcome, "changed", changed_fallback))
    except Exception as exc:
        raise RecoveryFailed(f"recovered changed unreadable: {exc}") from exc
    proof = _proof_from_kernel_payload(item)
    return RecoveredResultSpec(
        call=call_obj,
        model_text=model_text,
        audit=audit_dict,
        presentation=presentation,
        canonical=canonical,
        truncated=truncated,
        trusted_workspace=proof,
    )


def _proof_from_kernel_payload(item: Any) -> Any | None:
    """Extract a verified proof from the kernel-owned row payload only."""
    from codey.operations.kernel_provenance import TrustedWorkspaceProof

    payload = getattr(item, "workspace_identity", None)
    # Legacy shape: separate kernel-owned revision/fingerprint fields beside audit.
    if payload is None:
        rev = getattr(item, "workspace_revision", None)
        fp = getattr(item, "workspace_fingerprint", None)
        if rev is None and fp is None:
            return None
        try:
            from codey.workspace.revision import WorkspaceIdentity

            payload = WorkspaceIdentity.trusted_pair(rev, fp)
        except Exception:
            return None
    try:
        trusted = bool(getattr(payload, "trusted", False))
    except Exception:
        return None
    if not trusted:
        return None
    return TrustedWorkspaceProof(identity=payload, source="frame_kernel_payload")


def spec_from_memory_result(stored: ToolResult, call: ToolCall) -> RecoveredResultSpec:
    """Adapter for in-memory settled results (side-channel copy only)."""
    from codey.operations.kernel_provenance import (
        TrustedWorkspaceProof,
        _kernel_workspace_identity_of,
    )

    try:
        identity = _kernel_workspace_identity_of(stored)
    except Exception:
        identity = None
    proof = (
        TrustedWorkspaceProof(identity=identity, source="in_memory_kernel_result")
        if identity is not None and bool(getattr(identity, "trusted", False))
        else None
    )
    try:
        audit = dict(getattr(stored, "audit", {}) or {})
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    try:
        presentation = dict(getattr(stored, "presentation", {}) or {})
    except Exception:
        presentation = {}
    try:
        canonical = dict(getattr(stored, "canonical", {}) or {})
    except Exception:
        canonical = {}
    try:
        truncated = bool(getattr(stored, "truncated", False))
        model_text = str(getattr(stored, "model_text", "") or "")
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
    from codey.operations.kernel_provenance import TrustedWorkspaceProof

    proof = None
    if verified_identity is not None and bool(getattr(verified_identity, "trusted", False)):
        proof = TrustedWorkspaceProof(identity=verified_identity, source="persisted_revision_store")
    try:
        excerpt = str(record.get("excerpt", "") or "") if isinstance(record, dict) else ""
    except Exception as exc:
        raise RecoveryFailed(f"persisted replay unreadable: {exc}") from exc
    audit: dict = {}
    try:
        name = str(record.get("name", "") or "") if isinstance(record, dict) else ""
        if name.strip().lower() == "edit":
            audit["changed"] = True
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
