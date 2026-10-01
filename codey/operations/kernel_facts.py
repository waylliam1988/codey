"""Fact recording for kernel tool results (no executor/recovery dependency)."""

from __future__ import annotations

from typing import Any

from codey.operations.kernel_protocol import _CONTROLLER_ALIASES
from codey.operations.kernel_provenance import _kernel_workspace_identity_of, _session_workspace_identity
from codey.operations.kernel_result import strict_exit_code_or_none
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "record_facts_for_result",
]


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
    for hid, target in mapping.items():
        if type(hid) is not str or not hid:
            raise ValueError("hit id must be a non-empty string")
        if not isinstance(target, dict):
            raise ValueError("hit target must be a mapping")
        url = str(target.get("url", "") or "").strip()[:500]
        offset = target.get("offset", 0)
        pages = str(target.get("pages", "") or "")[:40]
        if not url or type(offset) is not int or offset < 0 or type(pages) is not str:
            raise ValueError("hit target is malformed")
        clean = {"url": url, "offset": offset, "pages": pages}
        existing = (getattr(session, "hit_targets", {}) or {}).get(hid)
        if existing is not None:
            if existing != clean:
                raise ValueError(f"conflicting hit target for {hid}")
            continue
        session.hit_targets[hid] = clean


def _record_open_fact(session: TaskSession, name: str, args: dict[str, Any], result: ToolResult) -> None:
    canonical = _canonical_mapping(result)
    opened_url = str(canonical.get("opened_url", "") or "").strip()[:500]
    if name == "open_url":
        url = (opened_url or str(args.get("url", "") or "")).strip()
        if url:
            session.record_open(url)
        return
    url = opened_url
    if not url:
        key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}[name]
        rid = str(args.get(key, "") or "").strip().lower()
        if name == "open_hit":
            hit_url = str(((getattr(session, "hit_targets", {}) or {}).get(rid, {}) or {}).get("url", "") or "").strip()
            url = (hit_url or session.search_results.get(rid, "") or session.source_ids.get(rid, "")).strip()
        else:
            url = (session.search_results.get(rid, "") or session.source_ids.get(rid, "")).strip()
    if url:
        session.record_open(url)


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
            session.record_evidence(url[:500], excerpt[:600])


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
    exit_code: int | None = None,
) -> None:
    name = str(call.name or "").strip().lower()
    args = call.args if isinstance(call.args, dict) else {}
    text = str(result.model_text or "")
    sess_rev, sess_fp = _session_workspace_identity(session)
    if name == "run":
        # Single record path: every executed run is an observation, known or
        # unknown. Unknown (missing/invalid exit) records passed=False with
        # no exit_code so the latest observation blocks instead of reviving
        # an older success. Text never implies pass.
        effective_exit = strict_exit_code_or_none(exit_code) if exit_code is not None else None
        if (
            effective_exit is None
            and isinstance(result.audit, dict)
            and result.audit.get("exit_code") is not None
        ):
            effective_exit = strict_exit_code_or_none(result.audit.get("exit_code"))
        identity = _kernel_workspace_identity_of(result)
        if identity is not None:
            sess_rev, sess_fp = identity.revision, identity.fingerprint
        elif session.project:
            # A recovered observation cannot borrow the resumed version.
            sess_rev, sess_fp = 0, ""
        _record_run_verification(session, args, effective_exit, sess_rev, sess_fp, ok=ok)
        if text:
            session.transcript_notes.append(f"run: {text[:500]}")
        return
    if name == "source_search":
        if not ok:
            if text:
                session.transcript_notes.append(f"{name}: {text[:500]}")
            return
        _apply_hit_targets(session, _canonical_mapping(result).get("hit_targets"))
        if text:
            session.transcript_notes.append(f"{name}: {text[:500]}")
        return
    if not ok:
        return
    if name == "web_search":
        _record_search_results(session, args, text)
    elif name == "open_url" or name in _CONTROLLER_ALIASES:
        _record_open_fact(session, name, args, result)
    elif name == "knowledge_write":
        _record_knowledge_fact(session, result)
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
