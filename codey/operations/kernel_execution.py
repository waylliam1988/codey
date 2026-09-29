"""Single-turn tool execution for the task kernel."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from typing import Any

from codey.operations.kernel_protocol import _CONTROLLER_ALIASES, _policy_allows
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = ["execute_turn", "record_facts_for_result", "sync_workspace_state_after_edit"]

def _result_ok(name: str, result: ToolResult, *, exit_code: int | None = None) -> bool:
    if exit_code is not None:
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    text = str(result.model_text or "")
    return not (text.startswith("ERROR:") or text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"))


def _error_result(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(call=call, model_text=f"ERROR: {message}")


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
        call_id = str(getattr(call, "call_id", "") or full.call.call_id or "")
        return ToolResult(
            call=ToolCall(name=full.call.name, args=dict(full.call.args), call_id=call_id),
            model_text=full.model_text,
        )
    record = session.executed[identity]
    stored_name = str(record.get("name", "") or "")
    stored_digest = str(record.get("args_digest", "") or "")
    if not _same_effect_call(stored_name, stored_digest, call):
        return _recovery_mismatch_result(call, stored_name, stored_digest or "unknown-args")
    call_id = str(getattr(call, "call_id", "") or record.get("call_id", ""))
    return ToolResult(
        call=ToolCall(
            name=str(record.get("name", "") or name),
            args=dict(call.args if isinstance(call.args, dict) else {}),
            call_id=call_id,
        ),
        model_text=f"ERROR: already settled in turn {active_turn}; see prior delivery"
        if not bool(record.get("ok", False))
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
) -> None:
    """Sync one authoritative post-edit WorkspaceState to session+evidence.

    Single bounded fingerprint scan with the configured ``ignored_paths``.
    When ``revision_store`` is supplied, the durable ``bump_state`` is the
    single authority (one scan inside the store); otherwise only the
    observed fingerprint is aligned and the outer hooks bump owns the
    revision (at most two bounded scans per edit total, never divergent
    ignores).
    """
    try:
        ignores = tuple(str(p) for p in (ignored_paths or ())) if ignored_paths else ()
    except Exception:
        ignores = ()
    if revision_store is not None and project_path is not None:
        try:
            state = revision_store.bump_state(project_path, ignored_paths=ignores)
            rev, fp = int(state.revision or 0), str(state.fingerprint or "")
            if fp:
                with contextlib.suppress(Exception):
                    session.set_workspace_state(rev, fp)
                try:
                    if execution_evidence is not None and hasattr(execution_evidence, "set_workspace_state"):
                        execution_evidence.set_workspace_state(rev, fp)
                except Exception:
                    pass
                return
        except Exception:
            pass
    _sync_workspace_after_edit(session, project_path, execution_evidence, ignored_paths=ignores)


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
    try:
        passed = int(exit_code) == 0
    except (TypeError, ValueError):
        passed = False
    session.record_verification(
        str(args.get("command", "") or ""),
        latest,
        passed,
        exit_code=exit_code,
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
        effective_exit = exit_code
        if effective_exit is None and isinstance(result.audit, dict):
            try:
                if result.audit.get("exit_code") is not None:
                    effective_exit = int(result.audit.get("exit_code"))
            except (TypeError, ValueError):
                effective_exit = None
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
    return ToolResult(call=call, model_text=stored.model_text)


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
    """Execute via delegate or raw executor; returns (result, ok, opened, evidence, exit, handled)."""
    if delegate is not None and delegate.handles(name):
        result, ok, opened, evidence, exit_code = delegate.execute(
            call,
            turn=active_turn,
            tool_index=tool_index,
        )
        return result, ok, opened, evidence, exit_code, True
    fn = runnable.get(name)
    if fn is None:
        return _error_result(call, f"unknown tool executor: {name or '?'}"), False, "", [], None, True
    try:
        produced = fn(call)
    except Exception as exc:
        return _error_result(call, str(exc) or "tool failed"), False, "", [], None, True
    if isinstance(produced, ToolResult):
        result = produced
    elif isinstance(produced, str):
        result = ToolResult(call=call, model_text=produced)
    else:
        result = ToolResult(call=call, model_text=str(produced))
    ok = _result_ok(name, result)
    # ``run`` without a structured exit code never counts as verified pass;
    # verification is decided in record_facts_for_result from exit codes only.
    return result, ok, "", [], None, True


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
    for offset, call in enumerate(calls or []):
        name = str(getattr(call, "name", "") or "").strip().lower()
        identity = turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset)
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
        settle(identity, call, result, ok=ok)
        record_facts_for_result(
            session, call, result, ok=ok, opened_url=opened, evidence_items=evidence, exit_code=exit_code
        )
        # After a confirmed edit, sync the real post-edit file identity into
        # both the session and the outer evidence before any later run
        # receipt in the same batch or the next turn. Single observation
        # with the configured ignores; revision owned by the outer store
        # unless an explicit store is injected (single authoritative bump).
        if name == "edit" and ok:
            try:
                changed = True
                if isinstance(result.audit, dict) and "changed" in result.audit:
                    changed = bool(result.audit.get("changed"))
                if changed:
                    sync_workspace_state_after_edit(
                        session, project_path, execution_evidence,
                        ignored_paths=workspace_ignored_paths,
                        revision_store=workspace_revision_store,
                    )
            except Exception:
                pass
        results.append(result)
    return results


def _settle_slot(session: TaskSession, identity: str, call: ToolCall, result: ToolResult, *, ok: bool) -> None:
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        session.executed[identity] = {
            "name": str(result.call.name or ""),
            "ok": bool(ok),
            "call_id": str(result.call.call_id or getattr(call, "call_id", "") or ""),
            "excerpt": str(result.model_text or "")[:500],
            "args_digest": _call_args_digest(call),
        }

