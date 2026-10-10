"""Single normalization boundary for every ToolResult."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from codey.operations.kernel_provenance import _EXECUTOR_STRIPPED_AUDIT_KEYS
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "_consistent_tool_result",
    "_error_result",
    "_normalize_audit_exit_code",
    "_normalize_delegate_result",
    "_normalize_explicit_result",
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
    """Structured status plus strict exit identity; display text has no authority."""
    if not result.ok:
        return False
    audit_raw = result.audit.get("exit_code")
    audit_code = strict_exit_code_or_none(audit_raw)
    explicit_code = strict_exit_code_or_none(exit_code)
    if ((audit_raw is not None and audit_code is None)
            or (exit_code is not None and explicit_code is None)):
        return False
    if explicit_code is not None and audit_code is not None and explicit_code != audit_code:
        return False
    code = explicit_code if explicit_code is not None else audit_code
    return code == 0 if str(name or "").strip().lower() == "run" else True


def _strip_executor_workspace_audit(audit: object) -> dict[str, Any]:
    """Remove executor-forgeable workspace provenance from an audit dict."""
    try:
        source = dict(audit) if isinstance(audit, dict) else {}
    except Exception:
        return {}
    for key in _EXECUTOR_STRIPPED_AUDIT_KEYS:
        source.pop(key, None)
    return source


def _normalize_audit_exit_code(audit: dict[str, Any]) -> tuple[dict[str, Any], int | None, bool]:
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
    ok: bool,
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
    added here: only ``attach_trusted_workspace`` may attach provenance
    via the side-channel.
    """
    from codey.operations.kernel_errors import RecoveryFailed


    if audit is None:
        audit_dict: dict[str, Any] = {}
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
        presentation_dict: dict[str, Any] = {}
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
        canonical_dict: dict[str, Any] = {}
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
            ok=ok, call=call,
            model_text=text,
            truncated=truncated_flag,
            presentation=presentation_dict,
            audit=audit_dict,
            canonical=canonical_dict,
        )
    except Exception as exc:
        raise RecoveryFailed(f"recovered result rebuild failed: {exc}") from exc


def _error_result(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(ok=False, call=call, model_text=f"ERROR: {message}")


def policy_denial_result(call: ToolCall, message: str) -> ToolResult:
    """Runtime guard receipt: the executor has not been invoked."""
    return ToolResult(ok=False, call=call, model_text=f"ERROR: {message}",
                      audit={"execution_disposition": "denied_before_execution"})


def denied_before_execution(result: ToolResult) -> bool:
    """Only normalized runtime receipts may carry this disposition."""
    return (result.ok is False
            and result.audit.get("execution_disposition") == "denied_before_execution"
            and result.audit.get("exit_code") is None)


def _call_args_digest(call: ToolCall) -> str:
    try:
        from codey.runtime.effects.effect_records import compute_args_digest
    except Exception:
        return ""
    try:
        args = call.args if isinstance(call.args, dict) else dict[str, object]()
        return str(compute_args_digest(args) or "")
    except Exception:
        return ""


def _consistent_tool_result(
    requested: ToolCall, produced: ToolResult, *, runtime_guard: bool = False,
) -> ToolResult:
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
        if not runtime_guard:
            cleaned_audit.pop("execution_disposition", None)
        return ToolResult(
            ok=produced.ok, call=requested,
            model_text=produced.model_text,
            truncated=bool(produced.truncated),
            presentation=dict(produced.presentation) if isinstance(produced.presentation, dict) else {},
            audit=cleaned_audit,
            canonical=dict(produced.canonical) if isinstance(produced.canonical, dict) else {},
        )
    except Exception:
        return _error_result(requested, "tool result normalization failed; refusing to lose receipt")


def _normalize_explicit_result(name: str, result: ToolResult) -> tuple[ToolResult, bool]:
    """Finalize status before settlement and trusted workspace attachment."""
    cleaned_audit, _code, invalid = _normalize_audit_exit_code(dict(result.audit))
    ok = not invalid and result_ok(name, result)
    result = replace(result, audit=cleaned_audit, ok=ok)
    return result, result.ok


def _normalize_delegate_result(
    name: str, result: ToolResult, ok: bool, exit_code: int | None,
) -> tuple[ToolResult, bool, int | None]:
    """A delegate's status must agree with its result; unknown exits fail closed."""
    audit, _audit_code, invalid_audit = _normalize_audit_exit_code(dict(result.audit))
    valid_exit = strict_exit_code_or_none(exit_code)
    invalid_exit = exit_code is not None and valid_exit is None
    final_ok = (type(ok) is bool and ok and result.ok and not invalid_audit and not invalid_exit
                and result_ok(name, result, exit_code=exit_code))
    return replace(result, audit=audit, ok=final_ok), final_ok, valid_exit
