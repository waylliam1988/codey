"""Fact recording for kernel tool results (no executor/recovery dependency)."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from codey.operations.kernel_errors import EffectSettlementFailed
from codey.operations.kernel_protocol import _CONTROLLER_ALIASES
from codey.operations.kernel_provenance import _kernel_workspace_identity_of, _session_workspace_identity
from codey.operations.kernel_result import _call_args_digest, denied_before_execution, strict_exit_code_or_none
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import argument_digest
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import (
    restore_ledger_observation,
    search_observation,
    source_observation,
    source_search_observation,
)
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import canonical_tool_name

__all__ = [
    "record_facts_for_result",
    "project_result_observation",
]


def project_result_observation(
    session: TaskSession, call: ToolCall, result: ToolResult, *, ok: bool,
    exit_code: int | None = None, research_ledger: ResearchLedger | None = None,
) -> None:
    """The shared live/recovery projection, applied only to staged views."""
    observation = result.canonical.get("research_observation")
    if ok and research_ledger is not None and observation is not None:
        if not isinstance(observation, dict):
            raise ValueError("research observation must be an object")
        restore_ledger_observation(research_ledger, observation)
    record_facts_for_result(session, call, result, ok=ok, exit_code=exit_code)


def _record_run_verification(
    session: TaskSession, args: dict[str, Any], exit_code: int | None, sess_rev: int, sess_fp: str,
    *, ok: bool,
) -> None:
    latest = max([0, *list(session.edited_files.values())]) if session.edited_files else 0
    code = strict_exit_code_or_none(exit_code)
    # Unknown exits are still an executed observation: fail closed without
    # inventing 0/1. A missing code never passes, even when ok is True.
    passed = ok is True and code == 0
    session.record_verification(
        str(args.get("command", "") or ""),
        latest,
        passed,
        exit_code=code,
        workspace_revision=sess_rev or None,
        workspace_fingerprint=sess_fp or None,
        cwd=str(args.get("path") or "."),
    )


def _canonical_mapping(result: ToolResult) -> dict[str, Any]:
    try:
        canonical = getattr(result, "canonical", {}) or {}
        return dict(canonical) if isinstance(canonical, dict) else {}
    except Exception:
        return {}


def _apply_hit_targets(session: TaskSession, mapping: object) -> None:
    if mapping is None:
        return
    if not isinstance(mapping, dict):
        raise ValueError("hit_targets must be a mapping")
    # Two-phase: validate raw types/content and all conflicts into a local
    # pending set first; only commit once the whole batch passes. Never wash
    # illegal types via str() and never leave partial state behind.
    pending: dict[str, dict[str, object]] = {}
    for hid, target in mapping.items():
        if type(hid) is not str or not hid:
            raise ValueError("hit id must be a non-empty string")
        if not isinstance(target, dict):
            raise ValueError("hit target must be a mapping")
        raw_url = target.get("url", "")
        if type(raw_url) is not str:
            raise ValueError("hit target url must be a string")
        raw_offset = target.get("offset", 0)
        if type(raw_offset) is not int or raw_offset < 0:
            raise ValueError("hit target offset must be a nonnegative integer")
        raw_pages = target.get("pages", "")
        if type(raw_pages) is not str:
            raise ValueError("hit target pages must be a string")
        url = raw_url.strip()
        pages = raw_pages
        if not url:
            raise ValueError("hit target is malformed")
        clean: dict[str, object] = {"url": url, "offset": raw_offset, "pages": pages}
        if hid in pending and pending[hid] != clean:
            raise ValueError(f"conflicting hit target for {hid}")
        pending[hid] = clean
    existing_all = dict(getattr(session, "hit_targets", {}) or {})
    for hid, clean in pending.items():
        existing = existing_all.get(hid)
        if existing is not None and existing != clean:
            raise ValueError(f"conflicting hit target for {hid}")
    for hid, clean in pending.items():
        if hid not in session.hit_targets:
            session.hit_targets[hid] = clean


def _record_open_fact(session: TaskSession, result: ToolResult) -> None:
    source, _, _ = source_observation(result.canonical.get("research_observation"))
    sid = result.canonical.get("source_id")
    if not isinstance(sid, str) or not re.fullmatch(r"s[1-9][0-9]*", sid):
        raise ValueError("source observation requires a committed source_id")
    session.record_open(source.final_url, source_id=sid)


def bind_source_identity(session: TaskSession, call: ToolCall, result: ToolResult, *, ok: bool) -> ToolResult:
    """Allocate under the commit lock; persist the binding in the same receipt."""
    if not ok or canonical_tool_name(call.name) not in {"open_url", *_CONTROLLER_ALIASES}:
        return result
    source, _, _ = source_observation(result.canonical.get("research_observation"))
    if "source_id" in result.canonical:
        return result
    return replace(result, canonical={**result.canonical, "source_id": session.source_id_for(source.final_url)})


def _record_knowledge_fact(session: TaskSession, result: ToolResult) -> None:
    session.notes_saved += 1
    rows = _canonical_mapping(result).get("evidence_items")
    if not isinstance(rows, list):
        return
    for item in rows:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url", "") or "").strip()
        excerpt = str(item.get("excerpt", "") or "").strip()
        if url and excerpt:
            session.record_evidence(url, excerpt[:600])


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


def _record_run_fact(session: TaskSession, args: dict[str, Any], result: ToolResult, *,
                     ok: bool, exit_code: int | None) -> None:
    text = (result.model_text or "")
    if denied_before_execution(result):
        if text:
            session.transcript_notes.append(f"run refused before execution: {text[:500]}")
        return
    # Unknown attempted runs remain observations. Never revive an older pass
    # by discarding an outcome whose execution/result is uncertain.
    effective_exit = strict_exit_code_or_none(exit_code) if exit_code is not None else None
    if effective_exit is None and isinstance(result.audit, dict):
        effective_exit = strict_exit_code_or_none(result.audit.get("exit_code"))
    sess_rev, sess_fp = _session_workspace_identity(session)
    identity = _kernel_workspace_identity_of(result)
    if identity is not None:
        sess_rev, sess_fp = identity.revision, identity.fingerprint
    elif session.project:
        # A recovered observation cannot borrow the resumed version.
        sess_rev, sess_fp = 0, ""
    _record_run_verification(session, args, effective_exit, sess_rev, sess_fp, ok=ok)
    if text:
        session.transcript_notes.append(f"run: {text[:500]}")


def record_facts_for_result(
    session: TaskSession,
    call: ToolCall,
    result: ToolResult,
    *,
    ok: bool,
    exit_code: int | None = None,
) -> None:
    # Effect intents use executor names (read/ls/search); live turns use the
    # model contract. Both observations must project through the same name.
    name = canonical_tool_name(call.name)
    args = call.args if isinstance(call.args, dict) else {}
    text = (result.model_text or "")
    if name == "edit" and _canonical_mapping(result).get("workspace_unconfirmed") is True:
        # A failed identity bump does not undo the file effect. This fact is
        # persisted in the ordinary receipt and replayed even when ok=False.
        session.record_edit(str(args.get("path", "") or "file"))
        if text:
            session.transcript_notes.append(f"edit: {text[:500]}")
        return
    if name == "run":
        _record_run_fact(session, args, result, ok=ok, exit_code=exit_code)
        return
    if name == "source_search":
        if not ok:
            if text:
                session.transcript_notes.append(f"{name}: {text[:500]}")
            return
        source_search_observation(result.canonical.get("research_observation"))
        _apply_hit_targets(session, _canonical_mapping(result).get("hit_targets"))
        if text:
            session.transcript_notes.append(f"{name}: {text[:500]}")
        return
    if not ok:
        return
    if name == "web_search":
        _record_search_results(session, result)
    elif name == "open_url" or name in _CONTROLLER_ALIASES:
        _record_open_fact(session, result)
    elif name == "knowledge_write":
        _record_knowledge_fact(session, result)
    elif name == "edit":
        _record_edit_fact(session, args, result)
    elif name == "read_file":
        _record_read_fact(session, args)
    if text:
        session.transcript_notes.append(f"{name}: {text[:500]}")


def _record_search_results(session: TaskSession, result: ToolResult) -> None:
    from codey.research.url_selection import source_candidate_skip_reason

    search = search_observation(result.canonical.get("research_observation"))
    session.record_search(search.query)
    for row in search.results:
        url = row.url
        if source_candidate_skip_reason(url):
            continue
        existing = next((key for key, value in session.search_results.items() if value == url), "")
        if not existing:
            existing = f"r{len(session.search_results) + 1}"
        session.record_search_result(existing, url)

def project_execution_record(
    session: TaskSession,
    identity: str,
    call: ToolCall,
    result: ToolResult,
    *,
    ok: bool,
    exit_code: object = None,
) -> None:
    """Project the execution index on a staged live or recovered session.

    ``session._memory_results`` stays a tolerant process-local cache (handled
    by the caller with suppress). This index becomes visible only after the
    authoritative sink commit. Recovery invokes this same projection on the
    full committed receipt; this dictionary is not a persistent authority.
    """
    try:
        record: dict[str, Any] = {
            "name": canonical_tool_name(result.call.name),
            "ok": ok if type(ok) is bool else False,
            "call_id": str(result.call.call_id or getattr(call, "call_id", "") or ""),
            "excerpt": (result.model_text or "")[:500],
            "args_digest": _call_args_digest(call),
            "argument_digests": {key: argument_digest(value) for key, value in call.args.items()},
        }
        children = result.canonical.get("child_effect_ids")
        if children is not None:
            if (not isinstance(children, list) or any(not isinstance(ref, str) or not ref for ref in children)
                    or len(set(children)) != len(children)):
                raise ValueError("invalid child effect references")
            record["child_effect_ids"] = list(children)
        if result.canonical.get("workspace_unconfirmed") is True:
            record["applied_edit"] = {"mutation_applied": True, "identity_status": "unconfirmed",
                                      "identity_reason": result.canonical["identity_reason"]}
        strict_exit = strict_exit_code_or_none(exit_code)
        if strict_exit is None and isinstance(result.audit, dict):
            strict_exit = strict_exit_code_or_none(result.audit.get("exit_code"))
        if strict_exit is not None:
            record["exit_code"] = strict_exit
        if denied_before_execution(result):
            record["execution_disposition"] = "denied_before_execution"
    except Exception as exc:
        raise EffectSettlementFailed(f"effect settlement failed for {identity}: {exc}") from exc
    # Minimal durable provenance: only the kernel side-channel is persisted,
    # never raw audit. Extraction stays tolerant (missing provenance fails
    # closed on later replay); the receipt write below is fail-hard.
    try:
        identity_obj = _kernel_workspace_identity_of(result)
        if identity_obj is not None and type(ok) is bool and ok is True:
            record["workspace_revision"] = identity_obj.revision
            record["workspace_fingerprint"] = identity_obj.fingerprint
    except Exception as exc:
        raise EffectSettlementFailed(f"effect settlement provenance failed for {identity}: {exc}") from exc
    try:
        session.executed[identity] = record
    except Exception as exc:
        raise EffectSettlementFailed(f"effect settlement failed for {identity}: {exc}") from exc
