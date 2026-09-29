"""Single-turn tool execution for the task kernel."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from typing import Any

from codey.operations.kernel_protocol import _CONTROLLER_ALIASES, _policy_allows
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "build_recovered_tool_result",
    "execute_turn",
    "record_facts_for_result",
    "sync_workspace_state_after_edit",
    "strict_exit_code_or_none",
]


# Kernel-owned workspace provenance travels outside the executor-controlled
# audit dict. Executor-provided ``workspace_revision``/``workspace_fingerprint``
# are stripped at the kernel boundary; only ``_with_trusted_workspace_state``
# may attach the authoritative pair via both audit keys (for durable display)
# and the private side-channel attribute read by the event projection.
_KERNEL_WORKSPACE_ATTR = "_kernel_workspace_identity"
_EXECUTOR_STRIPPED_AUDIT_KEYS = frozenset(
    {"workspace_revision", "workspace_fingerprint", "_kernel_workspace_trusted"}
)


def strict_exit_code_or_none(value: object) -> int | None:
    """Strict structured exit code: only real int passes (bool/str rejected)."""
    from codey.utils.refs import strict_exit_code

    return strict_exit_code(value)


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


def _kernel_workspace_identity_of(result: ToolResult) -> Any | None:
    """Return the kernel-attached trusted identity, else None.

    Only the private side-channel set by ``_with_trusted_workspace_state``
    counts. Audit-dict workspace keys alone are never trusted because an
    explicit executor can forge them.
    """
    try:
        identity = getattr(result, _KERNEL_WORKSPACE_ATTR, None)
        if identity is None:
            return None
        if bool(getattr(identity, "trusted", False)):
            return identity
    except Exception:
        pass
    return None


def _copy_kernel_workspace_provenance(src: ToolResult, dst: ToolResult) -> None:
    """Carry the kernel side-channel across a ToolResult rebuild."""
    try:
        identity = getattr(src, _KERNEL_WORKSPACE_ATTR, None)
        if identity is not None and bool(getattr(identity, "trusted", False)):
            object.__setattr__(dst, _KERNEL_WORKSPACE_ATTR, identity)
    except Exception:
        pass


def _is_unsafe_tool(name: str) -> bool:
    """True when a replay without full provenance must fail closed."""
    try:
        from codey.toolchain.tool_spec import spec_for_tool

        spec = spec_for_tool(str(name or "").strip().lower())
    except Exception:
        return True
    if spec is None:
        return True
    return str(getattr(spec, "replay_class", "unsafe") or "unsafe").strip().lower() != "safe"


def build_recovered_tool_result(
    call: ToolCall,
    *,
    model_text: object = "",
    audit: object = None,
    presentation: object = None,
    canonical: object = None,
    truncated: object = False,
) -> ToolResult:
    """Shared rebuild for recovered/delivered results (single helper).

    Preserves kernel-owned metadata (audit/presentation/canonical/truncated)
    so a recovered edit keeps its trusted workspace identity. Edit audits
    without an explicit ``changed`` flag inherit ``changed=False`` only when
    the caller passes no audit; callers that know the outcome changed must
    include it explicitly. Executor-forgeable workspace keys are never added
    here: only ``_with_trusted_workspace_state`` may attach provenance via
    the side-channel.
    """
    try:
        audit_dict = dict(audit) if isinstance(audit, dict) else {}
    except Exception:
        audit_dict = {}
    try:
        presentation_dict = dict(presentation) if isinstance(presentation, dict) else {}
    except Exception:
        presentation_dict = {}
    try:
        canonical_dict = dict(canonical) if isinstance(canonical, dict) else {}
    except Exception:
        canonical_dict = {}
    try:
        return ToolResult(
            call=call,
            model_text=str(model_text or ""),
            truncated=bool(truncated),
            presentation=presentation_dict,
            audit=audit_dict,
            canonical=canonical_dict,
        )
    except Exception:
        return ToolResult(call=call, model_text=str(model_text or ""), audit=audit_dict)


def _error_result(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(call=call, model_text=f"ERROR: {message}")


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


def _with_trusted_workspace_state(result: ToolResult, *, revision: int, fingerprint: str) -> ToolResult:
    """Attach the authoritative (revision, fingerprint) to one edit result.

    The pair is stored both in audit (durable display) and in the private
    side-channel. Only the side-channel is trusted downstream; audit keys
    alone never confer trust.
    """
    from codey.workspace.revision import WorkspaceIdentity

    identity = WorkspaceIdentity.trusted_pair(revision, fingerprint)
    if not identity.trusted:
        return result
    try:
        audit = dict(result.audit) if isinstance(result.audit, dict) else {}
    except Exception:
        audit = {}
    audit = identity.attach_to_audit(audit)
    try:
        trusted = ToolResult(
            call=result.call,
            model_text=result.model_text,
            truncated=bool(result.truncated),
            presentation=dict(result.presentation) if isinstance(result.presentation, dict) else {},
            audit=audit,
            canonical=dict(result.canonical) if isinstance(result.canonical, dict) else {},
        )
    except Exception:
        return result
    try:
        object.__setattr__(trusted, _KERNEL_WORKSPACE_ATTR, identity)
    except Exception:
        return result
    return trusted


def _trusted_workspace_from_result(result: ToolResult) -> tuple[int, str]:
    """Strict kernel provenance: side-channel only, never raw audit."""
    try:
        identity = _kernel_workspace_identity_of(result)
        if identity is not None:
            return int(identity.revision), str(identity.fingerprint)
    except Exception:
        pass
    return 0, ""


def _build_delegate(
    session: TaskSession,
    project_path: Any,
    tool_fns: Any,
    research_tools: Any,
    change_tracker: Any = None,
    managed_outputs: Any = None,
    session_id: str = "",
    run_id: str = "",
    permission_profile: str = "coding_writer",
) -> Any:
    if project_path is None and research_tools is None:
        return None
    try:
        from codey.operations.task_execution import ExecutionDelegate
    except Exception:
        return None
    try:
        return ExecutionDelegate(
            session=session,
            project_path=project_path,
            tool_fns=tool_fns,
            research_tools=research_tools,
            change_tracker=change_tracker,
            managed_outputs=managed_outputs,
            session_id=session_id,
            run_id=run_id,
            permission_profile=permission_profile,
        )
    except Exception:
        return None


def _skip_unsettled(intent_sink: Any, identity: str, name: str) -> bool:
    if intent_sink is None:
        return False
    from codey.toolchain.tool_spec import spec_for_tool

    spec = spec_for_tool(name)
    return bool(intent_sink.has_unsettled(identity)) and (spec is None or spec.replay_class != "safe")


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


def _same_effect_call(stored_name: str, stored_digest: str, call: ToolCall) -> bool:
    name = str(getattr(call, "name", "") or "").strip().lower()
    if str(stored_name or "").strip().lower() != name:
        return False
    if not stored_digest:
        # Legacy records without a digest cannot prove sameness; treat as
        # mismatch for unsafe tools to avoid mis-delivery. Safe reads fall
        # back to re-execution via the caller.
        return False
    return str(stored_digest or "") == _call_args_digest(call)


def _recovery_mismatch_result(call: ToolCall, expected: str, actual: str) -> ToolResult:
    return ToolResult(
        call=call,
        model_text=(
            "ERROR: recovery mismatch for this turn slot: "
            f"expected {expected or '?'} with args {actual or '?'}; "
            "the prior settled result was for a different tool/args and must not be reused. "
            "Stop this batch and re-issue the correct call."
        ),
    )


def _replay_settled_slot(
    session: TaskSession,
    identity: str,
    call: ToolCall,
    name: str,
    active_turn: int,
) -> ToolResult | None:
    if identity not in (session.executed or {}):
        return None
    # Same turn slot already settled (retry after crash before delivery):
    # answer without re-executing dangerous writes. Full results stay
    # process-local (never in the bounded payload). The slot is identical
    # only when tool name and args digest both match; otherwise fail closed
    # and never reuse the old result.
    full = session._memory_results.get(identity)
    if full is not None:
        stored_name = str(getattr(full.call, "name", "") or "")
        try:
            from codey.runtime.effects.effect_records import compute_args_digest as _digest

            stored_digest = str(_digest(full.call.args if isinstance(full.call.args, dict) else {}) or "")
        except Exception:
            stored_digest = ""
        if not _same_effect_call(stored_name, stored_digest, call):
            return _recovery_mismatch_result(call, stored_name, stored_digest)
        # Preserve the full trusted result (audit/presentation/canonical/
        # truncated), including the kernel-owned workspace identity. Dropping
        # them loses the (revision, fingerprint) and causes a second bump.
        call_id = str(getattr(call, "call_id", "") or full.call.call_id or "")
        try:
            rebuilt = ToolResult(
                call=ToolCall(name=full.call.name, args=dict(full.call.args), call_id=call_id),
                model_text=full.model_text,
                truncated=bool(full.truncated),
                presentation=dict(full.presentation) if isinstance(full.presentation, dict) else {},
                audit=dict(full.audit) if isinstance(full.audit, dict) else {},
                canonical=dict(full.canonical) if isinstance(full.canonical, dict) else {},
            )
        except Exception:
            return full
        _copy_kernel_workspace_provenance(full, rebuilt)
        return rebuilt
    record = session.executed[identity]
    stored_name = str(record.get("name", "") or "")
    stored_digest = str(record.get("args_digest", "") or "")
    if not _same_effect_call(stored_name, stored_digest, call):
        return _recovery_mismatch_result(call, stored_name, stored_digest or "unknown-args")
    call_id = str(getattr(call, "call_id", "") or record.get("call_id", ""))
    was_ok = bool(record.get("ok", False))
    # Persisted replay (no _memory_results, no delivered): the bounded
    # ``executed`` record carries the minimal provenance. Unsafe tools
    # without trusted provenance fail closed instead of replaying a bare
    # success that hooks would bump a second time.
    if was_ok and _is_unsafe_tool(name):
        try:
            from codey.workspace.revision import WorkspaceIdentity
        except Exception:
            WorkspaceIdentity = None  # type: ignore[assignment]
        persisted_rev: Any = record.get("workspace_revision", None)
        persisted_fp: Any = record.get("workspace_fingerprint", None)
        persisted_identity = None
        try:
            if WorkspaceIdentity is not None:
                persisted_identity = WorkspaceIdentity.trusted_pair(persisted_rev, persisted_fp)
        except Exception:
            persisted_identity = None
        if persisted_identity is None or not bool(getattr(persisted_identity, "trusted", False)):
            return ToolResult(
                call=ToolCall(
                    name=str(record.get("name", "") or name),
                    args=dict(call.args if isinstance(call.args, dict) else {}),
                    call_id=call_id,
                ),
                model_text=(
                    "ERROR: persisted unsafe result lacks workspace provenance; "
                    "refusing to replay as success"
                ),
            )
        try:
            audit = {"workspace_revision": int(persisted_identity.revision),
                     "workspace_fingerprint": str(persisted_identity.fingerprint)}
            if str(record.get("name", "") or name).strip().lower() == "edit":
                audit["changed"] = True
            rebuilt = ToolResult(
                call=ToolCall(
                    name=str(record.get("name", "") or name),
                    args=dict(call.args if isinstance(call.args, dict) else {}),
                    call_id=call_id,
                ),
                model_text=str(record.get("excerpt", "") or ""),
                audit=audit,
            )
        except Exception:
            return ToolResult(
                call=call,
                model_text="ERROR: persisted unsafe result lacks workspace provenance; refusing to replay as success",
            )
        with contextlib.suppress(Exception):
            object.__setattr__(rebuilt, _KERNEL_WORKSPACE_ATTR, persisted_identity)
        return rebuilt
    return ToolResult(
        call=ToolCall(
            name=str(record.get("name", "") or name),
            args=dict(call.args if isinstance(call.args, dict) else {}),
            call_id=call_id,
        ),
        model_text=f"ERROR: already settled in turn {active_turn}; see prior delivery"
        if not was_ok
        else str(record.get("excerpt", "") or ""),
    )


def _session_workspace_identity(session: TaskSession) -> tuple[int, str]:
    try:
        rev = int(getattr(session, "workspace_revision", 0) or 0)
    except Exception:
        rev = 0
    try:
        fp = str(getattr(session, "workspace_fingerprint", "") or "")
    except Exception:
        fp = ""
    return rev, fp


def _disk_workspace_fingerprint(project_path: Any, *, ignored_paths: Any = ()) -> str:
    """Real post-edit file identity; empty when it cannot be observed."""
    try:
        if project_path is None:
            return ""
        from pathlib import Path

        from codey.workspace.revision import workspace_fingerprint

        candidate = Path(str(project_path)).expanduser() if not isinstance(project_path, Path) else project_path
        try:
            if not candidate.is_dir():
                return ""
        except Exception:
            return ""
        try:
            ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
        except Exception:
            ignores = ()
        return str(workspace_fingerprint(candidate, ignored_paths=ignores) or "")
    except Exception:
        return ""


def sync_workspace_state_after_edit(
    session: TaskSession,
    project_path: Any,
    execution_evidence: Any = None,
    *,
    ignored_paths: Any = (),
    revision_store: Any = None,
) -> tuple[int, str]:
    """Sync one authoritative post-edit WorkspaceState to session+evidence.

    Returns a kernel-owned :class:`WorkspaceIdentity` as a ``(revision,
    fingerprint)`` tuple: a fully valid pair is trusted, ``(0, "")`` is
    explicitly untrusted and must never be emitted as new evidence.

    When ``revision_store`` is supplied, the durable ``bump_state`` is the
    single authority (one atomic scan inside the store lock) and the trusted
    pair is returned so result events can carry it to hooks; hooks must adopt
    it without a second bump. Otherwise only the observed fingerprint is
    aligned and the outer hooks bump owns the revision (compat path retained
    for tests only: it scans once here for session alignment and hooks scan
    again for the revision; production must pass a durable store to avoid the
    double scan; the no-store case never emits trusted state).

    A supplied store that fails (exception, invalid revision, or empty
    fingerprint) never falls back to the session-guess path: the session
    keeps its prior identity so the old revision is never paired with the
    new fingerprint as new evidence.
    """
    try:
        ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
    except Exception:
        ignores = ()
    if revision_store is not None and project_path is not None:
        try:
            state = revision_store.bump_state(project_path, ignored_paths=ignores)
        except Exception:
            return 0, ""
        try:
            from codey.workspace.revision import WorkspaceIdentity
        except Exception:
            return 0, ""
        identity = WorkspaceIdentity.trusted_pair(
            getattr(state, "revision", 0), getattr(state, "fingerprint", "")
        )
        if not identity.trusted:
            return 0, ""
        rev, fp = int(identity.revision), str(identity.fingerprint)
        with contextlib.suppress(Exception):
            session.set_workspace_state(rev, fp)
        try:
            if execution_evidence is not None and hasattr(execution_evidence, "set_workspace_state"):
                execution_evidence.set_workspace_state(rev, fp)
        except Exception:
            pass
        return rev, fp
    _sync_workspace_after_edit(session, project_path, execution_evidence, ignored_paths=ignores)
    return 0, ""


def _sync_workspace_after_edit(
    session: TaskSession,
    project_path: Any,
    execution_evidence: Any = None,
    *,
    ignored_paths: Any = (),
) -> None:
    """Write the same real WorkspaceState to session and outer evidence.

    Called after a confirmed edit result, before any later run receipt.
    The revision counter itself is owned by the outer
    WorkspaceRevisionStore (hooks bump exactly once); here we only sync the
    observed file fingerprint so verification never carries the stale
    pre-edit identity. No inference from the startup-cached fingerprint,
    no second revision bump. ``ignored_paths`` must match the outer store
    config so both scans describe the same files.
    """
    try:
        ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
    except Exception:
        ignores = ()
    try:
        fp = _disk_workspace_fingerprint(project_path, ignored_paths=ignores)
    except Exception:
        fp = ""
    if not fp:
        return
    try:
        rev = int(getattr(session, "workspace_revision", 0) or 0) or 1
    except Exception:
        rev = 1
    with contextlib.suppress(Exception):
        session.set_workspace_state(rev, fp)
    # Same observed state for the outer evidence object when the kernel owns
    # it (run_task_kernel passes completion_context["execution_evidence"]).
    # Hooks still own the durable revision bump; this only aligns the
    # fingerprint so both sides describe the same files.
    try:
        if execution_evidence is not None and hasattr(execution_evidence, "set_workspace_state"):
            try:
                outer_rev = int(getattr(execution_evidence, "workspace_revision", 0) or 0) or rev
            except Exception:
                outer_rev = rev
            # Never downgrade a newer outer revision; only align fingerprint.
            if outer_rev < rev:
                outer_rev = rev
            execution_evidence.set_workspace_state(outer_rev, fp)
    except Exception:
        pass


def _record_run_verification(
    session: TaskSession, args: dict[str, Any], exit_code: int | None, sess_rev: int, sess_fp: str
) -> None:
    latest = max([0, *list(session.edited_files.values())]) if session.edited_files else 0
    code = strict_exit_code_or_none(exit_code)
    if code is None:
        return
    passed = code == 0
    session.record_verification(
        str(args.get("command", "") or ""),
        latest,
        passed,
        exit_code=code,
        workspace_revision=sess_rev or None,
        workspace_fingerprint=sess_fp or None,
    )


def _record_open_fact(session: TaskSession, name: str, args: dict[str, Any], opened_url: str) -> None:
    if name == "open_url":
        url = (opened_url or str(args.get("url", "") or "")).strip()
        if url:
            session.record_open(url)
        return
    url = (opened_url or "").strip()
    if not url:
        key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}[name]
        rid = str(args.get(key, "") or "").strip().lower()
        url = (session.search_results.get(rid, "") or session.source_ids.get(rid, "")).strip()
    if url:
        session.record_open(url)


def _record_knowledge_fact(session: TaskSession, evidence_items: list[dict[str, str]] | None) -> None:
    session.notes_saved += 1
    for item in evidence_items or ():
        url = str(item.get("source_url", "") or "").strip()
        excerpt = str(item.get("excerpt", "") or "").strip()
        if url and excerpt:
            session.record_evidence(url, excerpt)


def _record_edit_fact(session: TaskSession, args: dict[str, Any], result: ToolResult) -> None:
    if not isinstance(result.audit, dict) or result.audit.get("changed", True):
        session.record_edit(str(args.get("path", "") or "file"))


def _record_read_fact(session: TaskSession, args: dict[str, Any]) -> None:
    try:
        from pathlib import Path

        from codey.agents.protocol import canonical_project_path

        session.read_files.add(canonical_project_path(Path(session.project), str(args.get("path") or "")))
    except (ValueError, OSError):
        pass


def record_facts_for_result(
    session: TaskSession,
    call: ToolCall,
    result: ToolResult,
    *,
    ok: bool,
    opened_url: str = "",
    evidence_items: list[dict[str, str]] | None = None,
    exit_code: int | None = None,
) -> None:
    name = str(call.name or "").strip().lower()
    args = call.args if isinstance(call.args, dict) else {}
    text = str(result.model_text or "")
    sess_rev, sess_fp = _session_workspace_identity(session)
    if name == "run":
        # Structured exit codes only; text never implies pass. Missing
        # structured exit stays not_run (no passing verification recorded).
        # Audit exit codes are structured when present; otherwise no record.
        # Bool/str exits are not structured and never record.
        effective_exit = strict_exit_code_or_none(exit_code) if exit_code is not None else None
        if (
            effective_exit is None
            and isinstance(result.audit, dict)
            and result.audit.get("exit_code") is not None
        ):
            effective_exit = strict_exit_code_or_none(result.audit.get("exit_code"))
        if effective_exit is not None:
            _record_run_verification(session, args, effective_exit, sess_rev, sess_fp)
            if text:
                session.transcript_notes.append(f"run: {text[:500]}")
            return
        if text:
            session.transcript_notes.append(f"run: {text[:500]}")
        return
    if not ok:
        return
    if name == "web_search":
        _record_search_results(session, args, text)
    elif name == "open_url" or name in _CONTROLLER_ALIASES:
        _record_open_fact(session, name, args, opened_url)
    elif name == "knowledge_write":
        _record_knowledge_fact(session, evidence_items)
    elif name == "edit":
        _record_edit_fact(session, args, result)
    elif name == "read_file":
        _record_read_fact(session, args)
    if text:
        session.transcript_notes.append(f"{name}: {text[:500]}")


def _search_result_rows(text: str) -> list[tuple[str, str]]:
    import re as _re

    rows: list[tuple[str, str]] = []
    for match in _re.finditer(r"https?://[^\s)>\]]+", str(text or "")):
        rows.append((f"r{len(rows) + 1}", match.group(0).rstrip(".,;)]")))
        if len(rows) >= 8:
            break
    return rows


def _record_search_results(session: TaskSession, args: dict[str, Any], text: str) -> None:
    session.record_search(str(args.get("query", "") or ""))
    for _rid, url in _search_result_rows(text):
        existing = next((key for key, value in session.search_results.items() if value == url), "")
        if not existing:
            existing = f"r{len(session.search_results) + 1}"
        session.record_search_result(existing, url)


def _delivered_slot_result(
    delivered_map: Mapping[str, ToolResult],
    identity: str,
    call: ToolCall,
) -> ToolResult | None:
    """Return the delivered recovery result or a mismatch error, else None."""
    if identity not in delivered_map:
        return None
    stored = delivered_map[identity]
    try:
        stored_call = getattr(stored, "call", None)
        stored_name = str(getattr(stored_call, "name", "") or "") if stored_call is not None else ""
        try:
            from codey.runtime.effects.effect_records import compute_args_digest as _d

            stored_args = getattr(stored_call, "args", {}) if stored_call is not None else {}
            stored_digest = str(_d(stored_args if isinstance(stored_args, dict) else {}) or "")
        except Exception:
            stored_digest = ""
    except Exception:
        stored_call, stored_name, stored_digest = None, "", ""
    if stored_call is None or not _same_effect_call(stored_name, stored_digest, call):
        return _recovery_mismatch_result(call, stored_name or "unknown", stored_digest or "unknown-args")
    # Preserve kernel-owned metadata (audit/presentation/canonical/truncated)
    # so a recovered edit keeps its trusted workspace identity.
    try:
        rebuilt = build_recovered_tool_result(
            call,
            model_text=stored.model_text,
            truncated=bool(getattr(stored, "truncated", False)),
            presentation=dict(stored.presentation) if isinstance(stored.presentation, dict) else {},
            audit=dict(stored.audit) if isinstance(stored.audit, dict) else {},
            canonical=dict(stored.canonical) if isinstance(stored.canonical, dict) else {},
        )
    except Exception:
        return stored
    _copy_kernel_workspace_provenance(stored, rebuilt)
    return rebuilt


def _is_recovery_mismatch_text(text: object) -> bool:
    return str(text or "").startswith("ERROR: recovery mismatch")


def _batch_recovery_mismatch(
    session: TaskSession,
    calls: list[ToolCall],
    delivered_map: Mapping[str, ToolResult],
    identity_ref: str,
    active_turn: int,
    base_index: int,
) -> bool:
    """Pre-check the whole batch before begin_turn; True means abort without side effects."""
    for offset, call in enumerate(calls or []):
        name = str(getattr(call, "name", "") or "").strip().lower()
        identity = turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset)
        try:
            delivered_hit = _delivered_slot_result(delivered_map, identity, call)
        except Exception:
            delivered_hit = None
        if delivered_hit is not None and _is_recovery_mismatch_text(delivered_hit.model_text):
            return True
        try:
            replayed = _replay_settled_slot(session, identity, call, name, active_turn)
        except Exception:
            replayed = None
        if replayed is not None and _is_recovery_mismatch_text(replayed.model_text):
            return True
    return False


def _batch_aborted_results(calls: list[ToolCall]) -> list[ToolResult]:
    """Native-legal errors for every call id when the batch is aborted; no settlement."""
    results: list[ToolResult] = []
    for call in calls or []:
        results.append(_recovery_mismatch_result(call, "batch", "batch-aborted"))
    return results


def _guarded_slot_result(
    session: TaskSession,
    identity: str,
    call: ToolCall,
    name: str,
    active_turn: int,
    intent_sink: Any,
    controller_allowed: Any,
) -> ToolResult | None:
    """Policy/controller/replay guards; None means proceed to real execution."""
    if _skip_unsettled(intent_sink, identity, name):
        return _error_result(call, f"interrupted {name} not re-executed; see prior intent")
    replayed = _replay_settled_slot(session, identity, call, name, active_turn)
    if replayed is not None:
        return replayed
    if not _policy_allows(session.policy, name):
        return _error_result(call, f"disallowed tool for this task policy: {name or '?'}")
    if controller_allowed is not None:
        try:
            from codey.operations.kernel_protocol import _controller_allows as _allows_ctl

            if not _allows_ctl(name, {str(n or "").strip().lower() for n in controller_allowed}):
                return _error_result(call, f"{name} is not allowed by the current controller state")
        except Exception:
            # Fail closed: controller evaluation failure never means unlimited.
            return _error_result(call, "controller state unavailable; cannot authorize tool")
    return None


def _explicit_policy_denial(
    delegate: Any, call: ToolCall
) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None, bool] | None:
    """Delegate guard for the explicit executor; fail-closed on outage."""
    if delegate is None:
        return None
    try:
        handles = bool(delegate.handles(call.name if hasattr(call, "name") else ""))
    except Exception:
        return (
            _error_result(call, "policy check unavailable; refusing to run executor"),
            False, "", [], None, True,
        )
    if not handles:
        return None
    try:
        check = getattr(delegate, "_policy_check", None)
        if callable(check):
            denied, message, _approval = check(call)
            if denied:
                return (
                    ToolResult(call=call, model_text=f"ERROR: {message}"),
                    False, "", [], None, True,
                )
    except Exception as exc:
        # Fail closed: a policy-check outage never authorizes the
        # explicit executor. The executor is not invoked.
        return (
            _error_result(call, f"policy check unavailable; refusing to run {call.name or '?'}: {exc}"),
            False, "", [], None, True,
        )
    return None


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
    """Strict delegate exits: non-int structured/audit exits fail closed."""
    if str(result.model_text or "").startswith("ERROR: tool call"):
        ok = False
    if name == "run" and exit_code is not None and strict_exit_code_or_none(exit_code) is None:
        exit_code = None
        ok = False
    try:
        delegate_audit = dict(result.audit) if isinstance(result.audit, dict) else {}
    except Exception:
        delegate_audit = {}
    cleaned_audit, _code, invalid = _normalize_audit_exit_code(delegate_audit)
    if invalid:
        with contextlib.suppress(Exception):
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
    elif name == "run" and exit_code is not None and not _result_ok(name, result, exit_code=exit_code):
        ok = False
    return result, ok, exit_code


def _run_via_delegate_or_fn(
    delegate: Any,
    runnable: Mapping[str, Any],
    session: TaskSession,
    call: ToolCall,
    name: str,
    *,
    active_turn: int,
    tool_index: int,
) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None, bool]:
    """Execute via explicit executor first, delegate as fallback.

    An injected ``executors`` entry wins so tests (and custom harnesses)
    are never silently ignored when a real ``project_path`` creates a
    delegate. Production project runs pass no executors, so the delegate
    still owns real tools. Injected fakes still pass the delegate's project
    guards (path traversal, policy deny) so safety is never bypassed.
    """
    fn = runnable.get(name)
    if fn is not None:
        denial = _explicit_policy_denial(delegate, call)
        if denial is not None:
            return denial
        try:
            produced = fn(call)
        except Exception as exc:
            return _error_result(call, str(exc) or "tool failed"), False, "", [], None, True
        if isinstance(produced, ToolResult):
            result = _consistent_tool_result(call, produced)
        elif isinstance(produced, str):
            result = ToolResult(call=call, model_text=produced)
        else:
            result = ToolResult(call=call, model_text=str(produced))
        result, ok = _normalize_explicit_result(name, result)
        return result, ok, "", [], None, True
    if delegate is not None and delegate.handles(name):
        result, ok, opened, evidence, exit_code = delegate.execute(
            call,
            turn=active_turn,
            tool_index=tool_index,
        )
        result = _consistent_tool_result(call, result)
        result, ok, exit_code = _normalize_delegate_result(name, result, ok, exit_code)
        return result, ok, opened, evidence, exit_code, True
    return _error_result(call, f"unknown tool executor: {name or '?'}"), False, "", [], None, True


def execute_turn(
    session: TaskSession,
    calls: list[ToolCall],
    *,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
    run_id: object = "",
    effect_scope: str = "",
    turn: object | None = None,
    tool_index_base: object = 0,
    project_path: Any = None,
    tool_fns: Any = None,
    research_tools: Any = None,
    change_tracker: Any = None,
    managed_outputs: Any = None,
    session_id: str = "",
    permission_profile: str = "coding_writer",
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: Any = None,
    controller_allowed: Any = None,
    execution_evidence: Any = None,
    workspace_ignored_paths: Any = (),
    workspace_revision_store: Any = None,
) -> list[ToolResult]:
    """Execute one turn; identity is run+turn+index with durable delivery first.

    The per-turn snapshot (policy ∩ controller) is enforced here as well as
    in parsing: research tools respect the controller, project tools never do.
    """

    runnable = dict(executors or {})
    try:
        active_turn = int(turn) if turn is not None else int(session.turn or 0)
    except (TypeError, ValueError):
        active_turn = int(session.turn or 0)
    try:
        base_index = int(tool_index_base or 0)
    except (TypeError, ValueError):
        base_index = 0
    run_ref = str(run_id or "")
    identity_ref = f"{run_ref}:{effect_scope}" if effect_scope else run_ref
    delivered_map = dict(delivered or {})
    # Pre-check the whole batch before begin_turn: any slot mismatch aborts
    # the batch, preserves original receipts/settlement, executes nothing.
    # Native chains still need an error for every call id in the batch.
    if calls and _batch_recovery_mismatch(
        session, list(calls), delivered_map, identity_ref or "adhoc", active_turn, base_index
    ):
        return _batch_aborted_results(list(calls))

    import contextlib as _contextlib

    def settle(identity: str, call: ToolCall, result: ToolResult, ok: bool) -> None:
        _settle_slot(session, identity, call, result, ok=ok)
        with _contextlib.suppress(Exception):
            session._memory_results[identity] = result
        if intent_sink is not None:
            intent_sink.settle(identity, bool(ok))

    results: list[ToolResult] = []
    delegate = _build_delegate(
        session,
        project_path,
        tool_fns,
        research_tools,
        change_tracker,
        managed_outputs,
        session_id,
        run_ref,
        permission_profile,
    )
    if intent_sink is not None and calls:
        pending_items = [
            (turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset), call, base_index + offset)
            for offset, call in enumerate(calls)
            if turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset) not in delivered_map
            and turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset) not in session.executed
        ]
        if pending_items:
            intent_sink.begin_turn(pending_items, turn=active_turn)
    workspace_unconfirmed = False
    for offset, call in enumerate(calls or []):
        name = str(getattr(call, "name", "") or "").strip().lower()
        identity = turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset)
        if workspace_unconfirmed:
            blocked = _error_result(
                call,
                "prior edit workspace identity unconfirmed (edit happened, revision not bumped); "
                "verification skipped, re-check workspace before continuing",
            )
            settle(identity, call, blocked, ok=False)
            results.append(blocked)
            continue
        delivered_hit = _delivered_slot_result(delivered_map, identity, call)
        if delivered_hit is not None:
            is_mismatch = str(getattr(delivered_hit, "model_text", "") or "").startswith("ERROR: recovery mismatch")
            settle(identity, call, delivered_hit, ok=not is_mismatch)
            results.append(delivered_hit)
            continue
        guarded = _guarded_slot_result(session, identity, call, name, active_turn, intent_sink, controller_allowed)
        if guarded is not None:
            text = str(getattr(guarded, "model_text", "") or "")
            if text.startswith("ERROR: recovery mismatch") or text.startswith("ERROR:"):
                settle(identity, call, guarded, ok=False)
            # Settled replay successes are already stored; do not re-settle.
            results.append(guarded)
            continue
        result, ok, opened, evidence, exit_code, _handled = _run_via_delegate_or_fn(
            delegate,
            runnable,
            session,
            call,
            name,
            active_turn=active_turn,
            tool_index=base_index + offset,
        )
        # Single settlement per effect: edits defer settlement until the
        # authoritative bump decides the final result. Settling ok=True first
        # and then ok=False on bump failure double-settles the same effect
        # and the durable ledger raises "effect already settled".
        edit_done = _settle_edit_with_workspace_bump(
            session, identity, call, name, result, ok,
            opened=opened, evidence=evidence, exit_code=exit_code,
            project_path=project_path, execution_evidence=execution_evidence,
            workspace_ignored_paths=workspace_ignored_paths,
            workspace_revision_store=workspace_revision_store,
            settle=settle, results=results,
        )
        if edit_done is not None:
            if edit_done == "unconfirmed":
                workspace_unconfirmed = True
            continue
        settle(identity, call, result, ok=ok)
        record_facts_for_result(
            session, call, result, ok=ok, opened_url=opened, evidence_items=evidence, exit_code=exit_code
        )
        results.append(result)
    return results


def _settle_edit_with_workspace_bump(
    session: TaskSession,
    identity: str,
    call: ToolCall,
    name: str,
    result: ToolResult,
    ok: bool,
    *,
    opened: str,
    evidence: list[dict[str, str]],
    exit_code: int | None,
    project_path: Any,
    execution_evidence: Any,
    workspace_ignored_paths: Any,
    workspace_revision_store: Any,
    settle: Any,
    results: list[ToolResult],
) -> str | None:
    """Settle one edit with its authoritative bump; None means not handled."""
    if name != "edit" or not ok:
        return None
    try:
        changed = True
        if isinstance(result.audit, dict) and "changed" in result.audit:
            changed = bool(result.audit.get("changed"))
    except Exception:
        changed = True
    if not changed:
        return None
    try:
        rev, fp = sync_workspace_state_after_edit(
            session, project_path, execution_evidence,
            ignored_paths=workspace_ignored_paths,
            revision_store=workspace_revision_store,
        )
    except Exception:
        rev, fp = 0, ""
    if rev and fp:
        trusted = _with_trusted_workspace_state(result, revision=rev, fingerprint=fp)
        settle(identity, call, trusted, ok=True)
        record_facts_for_result(
            session, call, trusted, ok=True, opened_url=opened,
            evidence_items=evidence, exit_code=exit_code,
        )
        results.append(trusted)
        return "trusted"
    if workspace_revision_store is not None:
        # Edit happened but identity unconfirmed: record the edit fact (file
        # did change), then settle ONCE as error and block same-batch work.
        with contextlib.suppress(Exception):
            _record_edit_fact(session, call.args if isinstance(call.args, dict) else {}, result)
        with contextlib.suppress(Exception):
            session.transcript_notes.append(f"edit: {str(result.model_text or '')[:500]}")
        failed = _error_result(
            call,
            "edit happened but workspace identity unconfirmed (revision bump failed); "
            "re-check workspace before verifying, do not re-apply the edit",
        )
        settle(identity, call, failed, ok=False)
        results.append(failed)
        return "unconfirmed"
    return None


def _settle_slot(session: TaskSession, identity: str, call: ToolCall, result: ToolResult, *, ok: bool) -> None:
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        record: dict[str, Any] = {
            "name": str(result.call.name or ""),
            "ok": bool(ok),
            "call_id": str(result.call.call_id or getattr(call, "call_id", "") or ""),
            "excerpt": str(result.model_text or "")[:500],
            "args_digest": _call_args_digest(call),
        }
        # Minimal durable provenance for persisted replay: the trusted
        # (revision, fingerprint) is persisted alongside the excerpt so a
        # restored session can replay the same identity without a second
        # bump. Only the kernel side-channel is persisted, never raw audit.
        try:
            identity_obj = _kernel_workspace_identity_of(result)
            if identity_obj is not None and bool(ok):
                record["workspace_revision"] = int(identity_obj.revision)
                record["workspace_fingerprint"] = str(identity_obj.fingerprint)
        except Exception:
            pass
        session.executed[identity] = record

