"""Fact recording for kernel tool results (no executor/recovery dependency)."""

from __future__ import annotations

from typing import Any

from codey.operations.kernel_protocol import _CONTROLLER_ALIASES
from codey.operations.kernel_provenance import _kernel_workspace_identity_of, _session_workspace_identity
from codey.operations.kernel_result import denied_before_execution, strict_exit_code_or_none
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import canonical_tool_name

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


def _record_open_fact(session: TaskSession, name: str, args: dict[str, Any], result: ToolResult) -> None:
    canonical = _canonical_mapping(result)
    opened_url = str(canonical.get("opened_url", "") or "").strip()
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
    text = str(result.model_text or "")
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
    text = str(result.model_text or "")
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
        _apply_hit_targets(session, _canonical_mapping(result).get("hit_targets"))
        if text:
            session.transcript_notes.append(f"{name}: {text[:500]}")
        return
    if not ok:
        return
    if name == "web_search":
        _record_search_results(session, args, text, _canonical_mapping(result))
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


def _record_search_results(
    session: TaskSession, args: dict[str, Any], text: str, canonical: dict[str, Any],
) -> None:
    observation = canonical.get("research_observation")
    if observation is not None:
        if not isinstance(observation, dict) or set(observation) != {"search"}:
            raise ValueError("invalid search observation")
        search = observation["search"]
        if not isinstance(search, dict) or not isinstance(search.get("results"), (list, tuple)):
            raise ValueError("invalid search results")
        from codey.research.url_selection import source_candidate_skip_reason

        urls = []
        for row in search["results"]:
            if not isinstance(row, dict) or type(row.get("url")) is not str or not row["url"]:
                raise ValueError("invalid search result URL")
            if not source_candidate_skip_reason(row["url"]):
                urls.append(row["url"])
    else:
        urls = [url for _, url in _search_result_rows(text)]
    session.record_search(str(args.get("query", "") or ""))
    for url in urls:
        existing = next((key for key, value in session.search_results.items() if value == url), "")
        if not existing:
            existing = f"r{len(session.search_results) + 1}"
        session.record_search_result(existing, url)
