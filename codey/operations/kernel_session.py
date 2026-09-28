"""Task session facts for the unified kernel: bounded refs, never raw output."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from codey.runtime.core.models import ToolCall, ToolResult
from codey.utils.refs import stable_ref


def effect_id_for_call(call: ToolCall) -> str:
    """Deprecated content hash kept for backward compatibility only.

    Production identity is run+turn+index (see ``turn_effect_id``): the same
    file may be read twice and the same command may re-run after an edit.
    """

    try:
        args_json = json.dumps(call.args if isinstance(call.args, dict) else {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        args_json = str(getattr(call, "args", {}))
    return stable_ref("task_effect", str(getattr(call, "name", "") or ""), args_json)


def turn_effect_id(run_id: object, turn: object, tool_index: object) -> str:
    """Call identity for intents: run + turn + index, never tool name+args.

    The same file may be read twice and the same command may re-run after an
    edit; those are new calls. Only the same turn slot reuses an identity.
    """

    try:
        return stable_ref("task_turn_effect", str(run_id or ""), int(turn or 0), int(tool_index or 0))
    except Exception:
        return stable_ref("task_turn_effect", str(run_id or ""), str(turn or 0), str(tool_index or 0))


@dataclass
class TaskSession:
    policy: Any
    task_kind: str = "project"
    project: str = ""
    max_turns: int = 8
    task_text: str = ""
    handoff: str = ""
    searches: list[str] = field(default_factory=list)
    search_results: dict[str, str] = field(default_factory=dict)
    opened_sources: set[str] = field(default_factory=set)
    source_ids: dict[str, str] = field(default_factory=dict)
    hit_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: list[dict[str, str]] = field(default_factory=list)
    edited_files: dict[str, int] = field(default_factory=dict)
    read_files: set[str] = field(default_factory=set)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    notes_saved: int = 0
    transcript_notes: list[str] = field(default_factory=list)
    last_done_text: str = ""
    turn: int = 0
    executed: dict[str, dict[str, Any]] = field(default_factory=dict)
    _memory_results: dict[str, ToolResult] = field(default_factory=dict, repr=False, compare=False)

    def record_search(self, query: str) -> None:
        text = str(query or "").strip()
        if text:
            self.searches.append(text[:240])

    def record_search_result(self, result_id: str, url: str) -> None:
        rid = str(result_id or "").strip().lower()
        target = str(url or "").strip()
        if rid and target:
            self.search_results[rid] = target[:500]

    def record_open(self, url: str, *, source_id: str = "") -> None:
        text = str(url or "").strip()
        if text:
            self.opened_sources.add(text[:500])
            sid = str(source_id or "").strip().lower()
            if not sid:
                sid = next((key for key, value in self.source_ids.items() if value == text[:500]), "")
            if not sid:
                sid = f"s{len(self.source_ids) + 1}"
            self.source_ids[sid] = text[:500]

    def record_evidence(self, source_url: str, excerpt: str) -> None:
        url = str(source_url or "").strip()
        clip = str(excerpt or "").strip()
        if url and clip:
            self.evidence.append({"source_url": url[:500], "excerpt": clip[:600]})

    def record_hit(self, url: str, *, offset: int = 0, pages: str = "") -> str:
        target = {"url": str(url or "")[:500], "offset": max(0, int(offset)),
                  "pages": str(pages or "")[:40]}
        existing = next((key for key, value in self.hit_targets.items() if value == target), "")
        hit_id = existing or f"h{len(self.hit_targets) + 1}"
        self.hit_targets[hit_id] = target
        return hit_id

    def record_edit(self, path: str, revision: int | None = None) -> int:
        key = str(path or "").strip() or "file"
        try:
            rev = int(revision) if revision is not None else -1
        except (TypeError, ValueError):
            rev = -1
        if rev < 0:
            current = max([0, *list(self.edited_files.values())])
            rev = current + 1
        self.edited_files[key] = rev
        return rev

    def record_verification(self, command: str, revision: int, passed: bool, *,
                            exit_code: int | None = None) -> None:
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            return
        import contextlib as _contextlib

        row: dict[str, Any] = {"command": str(command or "")[:240], "revision": rev, "passed": bool(passed)}
        if exit_code is not None:
            with _contextlib.suppress(TypeError, ValueError):
                row["exit_code"] = int(exit_code)
        self.verifications.append(row)

    def notes_text(self) -> str:
        parts = [*self.transcript_notes]
        if self.last_done_text:
            parts.append(self.last_done_text)
        return "\n".join(parts)

    def to_payload(self) -> dict[str, Any]:
        try:
            policy_payload = self.policy.to_payload() if hasattr(self.policy, "to_payload") else {}
        except Exception:
            policy_payload = {}
        # Bounded: receipts and refs only, never full tool outputs or file contents.
        notes = [str(item or "")[:500] for item in (self.transcript_notes or [])][-20:]
        executed_refs: dict[str, dict[str, Any]] = {}
        for key, record in (self.executed or {}).items():
            if not isinstance(record, dict):
                continue
            executed_refs[str(key)] = {
                "name": str(record.get("name", "") or "")[:80],
                "ok": bool(record.get("ok", False)),
                "call_id": str(record.get("call_id", "") or "")[:80],
                "excerpt": str(record.get("excerpt", "") or "")[:500],
            }
        return {
            "task_kind": str(self.task_kind or ""),
            "project": str(self.project or ""),
            "max_turns": int(self.max_turns or 0),
            "task_text": str(self.task_text or "")[:2000],
            "handoff": str(self.handoff or "")[:2000],
            "searches": [str(item or "")[:240] for item in (self.searches or [])][-20:],
            "search_results": {str(k): str(v)[:500] for k, v in list((self.search_results or {}).items())[-20:]},
            "opened_sources": sorted(str(u)[:500] for u in (self.opened_sources or set()))[-20:],
            "source_ids": {str(k): str(v)[:500] for k, v in list((self.source_ids or {}).items())[-20:]},
            "hit_targets": {str(k): {"url": str(v.get("url", ""))[:500],
                                      "offset": int(v.get("offset", 0) or 0),
                                      "pages": str(v.get("pages", ""))[:40]}
                            for k, v in list((self.hit_targets or {}).items())[-20:]
                            if isinstance(v, dict)},
            "evidence": [
                {"source_url": str(item.get("source_url", ""))[:500],
                 "excerpt": str(item.get("excerpt", ""))[:300]}
                for item in (self.evidence or []) if isinstance(item, dict)
            ][-20:],
            "edited_files": {str(k)[:240]: int(v) for k, v in (self.edited_files or {}).items()},
            "read_files": sorted(str(path)[:240] for path in self.read_files)[-40:],
            "verifications": [
                {"command": str(item.get("command", ""))[:240],
                 "revision": int(item.get("revision", 0) or 0),
                 "passed": bool(item.get("passed", False)),
                 "exit_code": item.get("exit_code", None)}
                for item in (self.verifications or []) if isinstance(item, dict)
            ][-20:],
            "notes_saved": int(self.notes_saved or 0),
            "transcript_notes": notes,
            "last_done_text": str(self.last_done_text or "")[:4000],
            "turn": int(self.turn or 0),
            "executed": executed_refs,
            "policy": policy_payload,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, Any] | None, *, policy: Any = None) -> TaskSession:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        active_policy = policy if policy is not None else data.get("policy")
        if not hasattr(active_policy, "allows"):
            try:
                from codey.policies.task_policy import TaskPolicy

                active_policy = TaskPolicy.from_payload(data.get("policy"))
            except Exception:
                active_policy = policy
        session = TaskSession(
            policy=active_policy,
            task_kind=str(data.get("task_kind", "") or "project"),
            project=str(data.get("project", "") or ""),
            max_turns=int(data.get("max_turns", 8) or 8),
        )
        try:
            session.task_text = str(data.get("task_text", "") or "")[:2000]
            session.handoff = str(data.get("handoff", "") or "")[:2000]
            session.searches = [str(i) for i in (data.get("searches", []) or []) if str(i)]
            raw_results = data.get("search_results", {}) or {}
            session.search_results = {str(k): str(v) for k, v in raw_results.items() if str(k) and str(v)}
            session.opened_sources = {str(i) for i in (data.get("opened_sources", []) or []) if str(i)}
            raw_sids = data.get("source_ids", {}) or {}
            session.source_ids = {str(k): str(v) for k, v in raw_sids.items() if str(k) and str(v)}
            raw_hits = data.get("hit_targets", {}) or {}
            session.hit_targets = {
                str(k): {"url": str(v.get("url", ""))[:500],
                         "offset": max(0, int(v.get("offset", 0) or 0)),
                         "pages": str(v.get("pages", ""))[:40]}
                for k, v in raw_hits.items() if isinstance(v, dict) and v.get("url")
            }
            session.evidence = [dict(i) for i in (data.get("evidence", []) or []) if isinstance(i, dict)]
            session.edited_files = {str(k): int(v) for k, v in (data.get("edited_files", {}) or {}).items()}
            session.read_files = {str(path) for path in (data.get("read_files", []) or []) if str(path)}
            session.verifications = [dict(i) for i in (data.get("verifications", []) or []) if isinstance(i, dict)]
            session.notes_saved = int(data.get("notes_saved", 0) or 0)
            session.transcript_notes = [str(i) for i in (data.get("transcript_notes", []) or [])]
            session.last_done_text = str(data.get("last_done_text", "") or "")
            session.turn = int(data.get("turn", 0) or 0)
            raw_executed = data.get("executed", {}) or {}
            session.executed = {str(k): dict(v) for k, v in raw_executed.items() if isinstance(v, dict)}
        except Exception:
            pass
        return session


__all__ = ["TaskSession", "effect_id_for_call", "turn_effect_id"]
