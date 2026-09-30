"""Fact recording for kernel tool results (no executor/recovery dependency)."""

from __future__ import annotations

from typing import Any

from codey.operations.kernel_protocol import _CONTROLLER_ALIASES
from codey.operations.kernel_provenance import _session_workspace_identity
from codey.operations.kernel_result import strict_exit_code_or_none
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "record_facts_for_result",
]


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
        cwd=str(args.get("path") or "."),
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
