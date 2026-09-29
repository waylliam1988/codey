"""Single normalization boundary for every ToolResult."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

from codey.operations.kernel_provenance import _EXECUTOR_STRIPPED_AUDIT_KEYS
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "_consistent_tool_result",
    "_error_result",
    "_normalize_audit_exit_code",
    "_normalize_delegate_result",
    "_normalize_explicit_result",
    "_result_ok",
    "_strip_executor_workspace_audit",
    "build_recovered_tool_result",
    "result_ok",
    "strict_exit_code_or_none",
]


def strict_exit_code_or_none(value: object) -> int | None:
    """Strict structured exit code: only real int passes (bool/str rejected)."""
    from codey.utils.refs import strict_exit_code

    return strict_exit_code(value)


def result_ok(name: str, result: ToolResult, *, exit_code: int | None = None) -> bool:
    """Public result verdict: structured exits decide, else ERROR/SKIPPED text."""
    return _result_ok(name, result, exit_code=exit_code)


def _result_ok(name: str, result: ToolResult, *, exit_code: int | None = None) -> bool:
    # Explicit exit_code param wins (delegate structured exit); otherwise the
    # audit exit_code is authoritative when present. Any present-but-invalid
    # exit (bool/str/float) fails closed. Valid run exits decide by code==0.
    audit_code: int | None = None
    audit_present_invalid = False
    try:
        audit = getattr(result, "audit", None)
        if isinstance(audit, dict) and "exit_code" in audit:
            raw = audit.get("exit_code")
            if raw is not None:
                parsed = strict_exit_code_or_none(raw)
                if parsed is None:
                    audit_present_invalid = True
                else:
                    audit_code = parsed
    except Exception:
        audit_present_invalid = False
    if exit_code is not None:
        code = strict_exit_code_or_none(exit_code)
        if code is None:
            return False
        if str(name or "").strip().lower() == "run":
            text = str(result.model_text or "")
            if text.startswith("ERROR:") or text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"):
                return False
            return code == 0
        text = str(result.model_text or "")
        return not (text.startswith("ERROR:") or text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"))
    if audit_present_invalid:
        return False
    if audit_code is not None and str(name or "").strip().lower() == "run":
        text = str(result.model_text or "")
        if text.startswith("ERROR:") or text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"):
            return False
        return audit_code == 0
    text = str(result.model_text or "")
    return not (text.startswith("ERROR:") or text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"))


def _strip_executor_workspace_audit(audit: object) -> dict:
    """Remove executor-forgeable workspace provenance from an audit dict."""
    try:
        source = dict(audit) if isinstance(audit, dict) else {}
    except Exception:
        return {}
    for key in _EXECUTOR_STRIPPED_AUDIT_KEYS:
        source.pop(key, None)
    return source


def _normalize_audit_exit_code(audit: dict) -> tuple[dict, int | None, bool]:
    """Split audit exit_code into (cleaned_audit, strict_code_or_None, invalid).

    Invalid means the key was present with a non-None value that is not a
    real int (bool/str/float rejected). Callers must fail closed on invalid
    and omit the key from downstream projections instead of coercing to 0.
    """
    try:
        raw_present = "exit_code" in audit
        raw = audit.get("exit_code") if raw_present else None
    except Exception:
        return dict(audit) if isinstance(audit, dict) else {}, None, False
    if not raw_present or raw is None:
        return audit, None, False
    code = strict_exit_code_or_none(raw)
    if code is None:
        cleaned = dict(audit)
        cleaned.pop("exit_code", None)
        return cleaned, None, True
    return audit, code, False


def build_recovered_tool_result(
    call: ToolCall,
    *,
    model_text: object = "",
    audit: object = None,
    presentation: object = None,
    canonical: object = None,
    truncated: object = False,
) -> ToolResult:
    """Shared rebuild for recovered/delivered results (single helper, strict).

    Missing/``None`` audit/presentation/canonical use ``{}``. Present-but-
    non-mapping or unreadable fields raise ``RecoveryFailed`` instead of
    silently becoming ``{}``. Executor-forgeable workspace keys are never
    added here: only ``with_trusted_workspace_state`` may attach provenance
    via the side-channel.
    """
    from codey.operations.kernel_errors import RecoveryFailed


    if audit is None:
        audit_dict: dict = {}
    elif not isinstance(audit, Mapping):
        raise RecoveryFailed(f"recovered audit must be a mapping, got {type(audit).__name__}")
    else:
        try:
            audit_dict = dict(audit)
        except Exception as exc:
            raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    for key in _EXECUTOR_STRIPPED_AUDIT_KEYS:
        audit_dict.pop(key, None)
    if presentation is None:
        presentation_dict: dict = {}
    elif not isinstance(presentation, Mapping):
        raise RecoveryFailed(
            f"recovered presentation must be a mapping, got {type(presentation).__name__}"
        )
    else:
        try:
            presentation_dict = dict(presentation)
        except Exception as exc:
            raise RecoveryFailed(f"recovered presentation unreadable: {exc}") from exc
    if canonical is None:
        canonical_dict: dict = {}
    elif not isinstance(canonical, Mapping):
        raise RecoveryFailed(
            f"recovered canonical must be a mapping, got {type(canonical).__name__}"
        )
    else:
        try:
            canonical_dict = dict(canonical)
        except Exception as exc:
            raise RecoveryFailed(f"recovered canonical unreadable: {exc}") from exc
    if model_text is None:
        text = ""
    elif type(model_text) is not str:
        raise RecoveryFailed(
            f"recovered model_text must be a string, got {type(model_text).__name__}"
        )
    else:
        text = model_text
    if truncated is None:
        truncated_flag = False
    elif type(truncated) is not bool:
        raise RecoveryFailed(
            f"recovered truncated must be a boolean, got {type(truncated).__name__}"
        )
    else:
        truncated_flag = truncated
    try:
        return ToolResult(
            call=call,
            model_text=text,
            truncated=truncated_flag,
            presentation=presentation_dict,
            audit=audit_dict,
            canonical=canonical_dict,
        )
    except Exception as exc:
        raise RecoveryFailed(f"recovered result rebuild failed: {exc}") from exc


def _error_result(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(call=call, model_text=f"ERROR: {message}")


def _call_args_digest(call: ToolCall) -> str:
    try:
        from codey.runtime.effects.effect_records import compute_args_digest
    except Exception:
        return ""
    try:
        args = call.args if isinstance(call.args, dict) else {}
        return str(compute_args_digest(args) or "")
    except Exception:
        return ""


def _consistent_tool_result(requested: ToolCall, produced: ToolResult) -> ToolResult:
    """Ensure a custom executor ToolResult reuses the requested call identity.

    Native chains receipt every requested call id; a rogue call with its own
    name/call_id/args would lose the receipt. Mismatches become an explicit
    error that reuses the original call (name/id/args from the request).
    Args are compared by digest so same name/id with a different path is
    rejected instead of silently accepted.
    """
    try:
        produced_call = getattr(produced, "call", None)
        if produced_call is None:
            return _error_result(requested, "tool returned no call; refusing to lose receipt")
        want_name = str(getattr(requested, "name", "") or "").strip().lower()
        got_name = str(getattr(produced_call, "name", "") or "").strip().lower()
        if want_name != got_name:
            return _error_result(
                requested,
                f"tool call mismatch: requested {want_name or '?'} got {got_name or '?'}; refusing to lose receipt",
            )
        want_id = str(getattr(requested, "call_id", "") or "")
        got_id = str(getattr(produced_call, "call_id", "") or "")
        if want_id != got_id:
            return _error_result(
                requested,
                f"tool call_id mismatch: requested {want_id or '?'} got {got_id or '?'}; refusing to lose receipt",
            )
        want_digest = _call_args_digest(requested)
        got_digest = _call_args_digest(produced_call)
        # An unavailable digest proves nothing: two empty digests must never
        # compare equal. Fail closed so a digest outage cannot accept a
        # divergent path as the requested call.
        if not want_digest or not got_digest or want_digest != got_digest:
            return _error_result(
                requested,
                "tool call args mismatch: returned args differ from requested args; refusing to lose receipt",
            )
    except Exception:
        return _error_result(requested, "tool call validation failed; refusing to lose receipt")
    # Normalize identity: validation success always rebuilds with the requested
    # call. Executor audit workspace keys are stripped here so only the kernel
    # bump (via _with_trusted_workspace_state) can re-attach provenance.
    try:
        cleaned_audit = _strip_executor_workspace_audit(
            dict(produced.audit) if isinstance(getattr(produced, "audit", None), dict) else {}
        )
        return ToolResult(
            call=requested,
            model_text=produced.model_text,
            truncated=bool(produced.truncated),
            presentation=dict(produced.presentation) if isinstance(produced.presentation, dict) else {},
            audit=cleaned_audit,
            canonical=dict(produced.canonical) if isinstance(produced.canonical, dict) else {},
        )
    except Exception:
        return _error_result(requested, "tool result normalization failed; refusing to lose receipt")


def _normalize_explicit_result(name: str, result: ToolResult) -> tuple[ToolResult, bool]:
    """Strip invalid audit exits; invalid forces ok=False (never 0)."""
    try:
        audit_dict = dict(result.audit) if isinstance(result.audit, dict) else {}
    except Exception:
        audit_dict = {}
    cleaned_audit, _strict_code, audit_invalid = _normalize_audit_exit_code(audit_dict)
    if audit_invalid:
        with contextlib.suppress(Exception):
            result = ToolResult(
                call=result.call,
                model_text=result.model_text,
                truncated=bool(result.truncated),
                presentation=dict(result.presentation) if isinstance(result.presentation, dict) else {},
                audit=cleaned_audit,
                canonical=dict(result.canonical) if isinstance(result.canonical, dict) else {},
            )
    ok = _result_ok(name, result)
    if audit_invalid:
        ok = False
    if str(result.model_text or "").startswith("ERROR: tool call"):
        ok = False
    return result, ok


def _normalize_delegate_result(
    name: str, result: ToolResult, ok: bool, exit_code: int | None
) -> tuple[ToolResult, bool, int | None]:
    """Strict delegate exits: non-int structured/audit exits fail closed.

    The structured ``exit_code`` parameter wins when present; otherwise an
    audit-only strict exit decides ``run`` verdicts. Present-but-invalid
    exits (bool/str/float) force ``ok=False`` and are stripped from audit.
    """
    if str(result.model_text or "").startswith("ERROR: tool call"):
        ok = False
    if name == "run" and exit_code is not None and strict_exit_code_or_none(exit_code) is None:
        exit_code = None
        ok = False
    try:
        delegate_audit = dict(result.audit) if isinstance(result.audit, dict) else {}
    except Exception:
        delegate_audit = {}
    cleaned_audit, audit_code, invalid = _normalize_audit_exit_code(delegate_audit)
    if invalid:
        with contextlib.suppress(Exception):
            from codey.operations.kernel_provenance import _copy_kernel_workspace_provenance

            rebuilt = ToolResult(
                call=result.call,
                model_text=result.model_text,
                truncated=bool(result.truncated),
                presentation=dict(result.presentation) if isinstance(result.presentation, dict) else {},
                audit=cleaned_audit,
                canonical=dict(result.canonical) if isinstance(result.canonical, dict) else {},
            )
            _copy_kernel_workspace_provenance(result, rebuilt)
            result = rebuilt
        ok = False
        return result, ok, exit_code
    if name == "run" and exit_code is not None and not _result_ok(name, result, exit_code=exit_code):
        ok = False
    elif name == "run" and exit_code is None and audit_code is not None and not _result_ok(name, result):
        # Audit-only structured exit is authoritative for run.
        ok = False
    return result, ok, exit_code
