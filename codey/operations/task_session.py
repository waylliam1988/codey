"""Task session facts for the unified kernel: bounded refs, never raw output."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from codey.runtime.core.models import ToolResult
from codey.utils.refs import stable_ref


def _validate_restored_receipts(
    raw_verifications: object, raw_executed: object
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    from codey.operations.kernel_errors import RecoveryFailed

    if not isinstance(raw_verifications, list):
        raise RecoveryFailed("session field verifications must be a list")
    verifications: list[dict[str, Any]] = []
    for item in raw_verifications:
        if not isinstance(item, Mapping):
            raise RecoveryFailed("session verification row must be a mapping")
        if "passed" in item and type(item["passed"]) is not bool:
            raise RecoveryFailed("session verification passed must be a boolean")
        if "exit_code" in item and item["exit_code"] is not None and type(item["exit_code"]) is not int:
            raise RecoveryFailed("session verification exit_code must be an integer")
        verifications.append(dict(item))
    if not isinstance(raw_executed, Mapping):
        raise RecoveryFailed("session field executed must be a mapping")
    executed: dict[str, dict[str, Any]] = {}
    for identity, row in raw_executed.items():
        if not isinstance(row, Mapping):
            raise RecoveryFailed(f"session executed receipt {identity!r} must be a mapping")
        if "ok" in row and type(row["ok"]) is not bool:
            raise RecoveryFailed("session executed ok must be a boolean")
        if "exit_code" in row and row["exit_code"] is not None and type(row["exit_code"]) is not int:
            raise RecoveryFailed("session executed exit_code must be an integer")
        executed[str(identity)] = dict(row)
    return verifications, executed


def _strict_text_list(value: object, field: str) -> list[str]:
    from codey.operations.kernel_errors import RecoveryFailed

    if not isinstance(value, list):
        raise RecoveryFailed(f"session field {field} must be a list")
    if any(type(item) is not str for item in value):
        raise RecoveryFailed(f"session field {field} must contain only strings")
    return [str(item) for item in value]


def _strict_text_mapping(value: object, field: str) -> dict[str, str]:
    from codey.operations.kernel_errors import RecoveryFailed

    if not isinstance(value, Mapping):
        raise RecoveryFailed(f"session field {field} must be a mapping")
    if any(type(key) is not str or type(item) is not str for key, item in value.items()):
        raise RecoveryFailed(f"session field {field} must contain only string pairs")
    return dict(value)


def _strict_evidence_list(value: object) -> list[dict[str, Any]]:
    from codey.operations.kernel_errors import RecoveryFailed

    if not isinstance(value, list):
        raise RecoveryFailed("session field evidence must be a list")
    if any(not isinstance(item, Mapping) for item in value):
        raise RecoveryFailed("session evidence rows must be mappings")
    return [dict(item) for item in value]


def _strict_hit_targets(value: object) -> dict[str, dict[str, Any]]:
    from codey.operations.kernel_errors import RecoveryFailed

    if not isinstance(value, Mapping):
        raise RecoveryFailed("session field hit_targets must be a mapping")
    targets: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if type(key) is not str or not isinstance(item, Mapping) or type(item.get("url")) is not str:
            raise RecoveryFailed("session hit target rows are malformed")
        offset = item.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise RecoveryFailed("session hit target offset must be a nonnegative integer")
        pages = item.get("pages", "")
        if type(pages) is not str:
            raise RecoveryFailed("session hit target pages must be a string")
        targets[key] = {"url": item["url"][:500], "offset": offset, "pages": pages[:40]}
    return targets


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
    # Explicit completion requirement from the task entry; never inferred
    # from keywords or write permission. Read-only tasks keep False even
    # when the policy still grants project.write for other reasons.
    project_changes_required: bool = False
    coding_context_enabled: bool = True
    # Current workspace identity observed by real edit/run execution.
    # Verifications carry the identity they observed; only a verification
    # whose fingerprint matches the current workspace can complete.
    workspace_revision: int = 0
    workspace_fingerprint: str = ""
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
        target = {"url": str(url or "")[:500], "offset": max(0, int(offset)), "pages": str(pages or "")[:40]}
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

    def record_verification(
        self,
        command: str,
        revision: int,
        passed: bool,
        *,
        exit_code: int | None = None,
        workspace_revision: int | None = None,
        workspace_fingerprint: str | None = None,
    ) -> None:
        if type(passed) is not bool:
            raise TypeError("verification passed must be a boolean")
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            return
        import contextlib as _contextlib

        row: dict[str, Any] = {"command": str(command or "")[:240], "revision": rev, "passed": passed}
        if exit_code is not None:
            try:
                from codey.utils.refs import strict_exit_code as _strict_exit

                code = _strict_exit(exit_code)
            except Exception:
                code = None
            if code is not None:
                row["exit_code"] = code
        if workspace_revision is not None:
            with _contextlib.suppress(TypeError, ValueError):
                row["workspace_revision"] = int(workspace_revision)
        if workspace_fingerprint is not None:
            row["workspace_fingerprint"] = str(workspace_fingerprint or "")[:120]
        self.verifications.append(row)

    def set_workspace_state(self, revision: object, fingerprint: object) -> None:
        try:
            from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision
        except Exception:
            return
        try:
            rev = valid_workspace_revision(revision)
            if rev:
                self.workspace_revision = rev
            fp = valid_workspace_fingerprint(fingerprint)
            # Empty fingerprint clears to missing (not_run), never faked.
            self.workspace_fingerprint = fp
        except Exception:
            pass

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
            row: dict[str, Any] = {
                "name": str(record.get("name", "") or "")[:80],
                "call_id": str(record.get("call_id", "") or "")[:80],
                "excerpt": str(record.get("excerpt", "") or "")[:500],
                "args_digest": str(record.get("args_digest", "") or "")[:80],
            }
            raw_ok = record.get("ok", False)
            row["ok"] = raw_ok if type(raw_ok) is bool else False
            if type(record.get("exit_code")) is int:
                row["exit_code"] = record["exit_code"]
            # Minimal durable provenance for unsafe replay: only a fully
            # valid (revision, fingerprint) pair is persisted.
            try:
                from codey.workspace.revision import (
                    valid_workspace_fingerprint as _valid_fp,
                )
                from codey.workspace.revision import valid_workspace_revision as _valid_rev
            except Exception:
                _valid_rev = None  # type: ignore[assignment]
                _valid_fp = None  # type: ignore[assignment]
            try:
                if _valid_rev is not None and _valid_fp is not None:
                    rev = _valid_rev(record.get("workspace_revision", 0))
                    fp = _valid_fp(record.get("workspace_fingerprint", ""))
                    if rev and fp:
                        row["workspace_revision"] = rev
                        row["workspace_fingerprint"] = fp
            except Exception:
                pass
            executed_refs[str(key)] = row
        return {
            "task_kind": str(self.task_kind or ""),
            "project": str(self.project or ""),
            "max_turns": int(self.max_turns or 0),
            "task_text": str(self.task_text or "")[:2000],
            "handoff": str(self.handoff or "")[:2000],
            "project_changes_required": bool(self.project_changes_required),
            "coding_context_enabled": bool(self.coding_context_enabled),
            "workspace_revision": int(self.workspace_revision or 0),
            "workspace_fingerprint": str(self.workspace_fingerprint or "")[:120],
            "searches": [str(item or "")[:240] for item in (self.searches or [])][-20:],
            "search_results": {str(k): str(v)[:500] for k, v in list((self.search_results or {}).items())[-20:]},
            "opened_sources": sorted(str(u)[:500] for u in (self.opened_sources or set()))[-20:],
            "source_ids": {str(k): str(v)[:500] for k, v in list((self.source_ids or {}).items())[-20:]},
            "hit_targets": {
                str(k): {
                    "url": str(v.get("url", ""))[:500],
                    "offset": int(v.get("offset", 0) or 0),
                    "pages": str(v.get("pages", ""))[:40],
                }
                for k, v in list((self.hit_targets or {}).items())[-20:]
                if isinstance(v, dict)
            },
            "evidence": [
                {"source_url": str(item.get("source_url", ""))[:500], "excerpt": str(item.get("excerpt", ""))[:300]}
                for item in (self.evidence or [])
                if isinstance(item, dict)
            ][-20:],
            "edited_files": {str(k)[:240]: int(v) for k, v in (self.edited_files or {}).items()},
            "read_files": sorted(str(path)[:240] for path in self.read_files)[-40:],
            "verifications": [
                {
                    "command": str(item.get("command", ""))[:240],
                    "revision": int(item.get("revision", 0) or 0),
                    "passed": (item.get("passed") if type(item.get("passed")) is bool else False),
                    "exit_code": item.get("exit_code", None),
                    "workspace_revision": int(item.get("workspace_revision", 0) or 0)
                    if item.get("workspace_revision") is not None
                    else None,
                    "workspace_fingerprint": str(item.get("workspace_fingerprint", "") or "")[:120]
                    if item.get("workspace_fingerprint")
                    else "",
                }
                for item in (self.verifications or [])
                if isinstance(item, dict)
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
        from codey.operations.kernel_errors import RecoveryFailed

        if payload is not None and not isinstance(payload, Mapping):
            raise RecoveryFailed("task session payload must be a mapping")
        data = dict(payload) if payload is not None else {}
        active_policy = policy if policy is not None else data.get("policy")
        if not hasattr(active_policy, "allows"):
            try:
                from codey.policies.task_policy import TaskPolicy

                active_policy = TaskPolicy.from_payload(data.get("policy"))
            except Exception as exc:
                raise RecoveryFailed(f"task session policy is malformed: {exc}") from exc
        try:
            session = TaskSession(
                policy=active_policy,
                task_kind=str(data.get("task_kind", "") or "project"),
                project=str(data.get("project", "") or ""),
                max_turns=int(data.get("max_turns", 8) or 8),
            )
        except Exception as exc:
            raise RecoveryFailed(f"task session header is malformed: {exc}") from exc

        def restore(field: str, parser: Any) -> Any:
            try:
                return parser()
            except RecoveryFailed:
                raise
            except Exception as exc:
                raise RecoveryFailed(f"session field {field} is malformed: {exc}") from exc

        session.task_text = restore("task_text", lambda: str(data.get("task_text", "") or "")[:2000])
        session.handoff = restore("handoff", lambda: str(data.get("handoff", "") or "")[:2000])
        session.project_changes_required = restore(
            "project_changes_required", lambda: data.get("project_changes_required", False) is True
        )
        session.coding_context_enabled = restore(
            "coding_context_enabled", lambda: data.get("coding_context_enabled", True) is True
        )
        session.workspace_revision = restore("workspace_revision", lambda: int(data.get("workspace_revision", 0) or 0))
        session.workspace_fingerprint = restore(
            "workspace_fingerprint", lambda: str(data.get("workspace_fingerprint", "") or "")[:120]
        )
        session.searches = restore("searches", lambda: _strict_text_list(data.get("searches", []) or [], "searches"))
        session.search_results = restore(
            "search_results", lambda: _strict_text_mapping(data.get("search_results", {}) or {}, "search_results")
        )
        session.opened_sources = set(
            restore("opened_sources", lambda: _strict_text_list(data.get("opened_sources", []) or [], "opened_sources"))
        )
        session.source_ids = restore(
            "source_ids", lambda: _strict_text_mapping(data.get("source_ids", {}) or {}, "source_ids")
        )
        session.hit_targets = restore(
            "hit_targets", lambda: _strict_hit_targets(data.get("hit_targets", {}) or {})
        )
        session.evidence = restore(
            "evidence", lambda: _strict_evidence_list(data.get("evidence", []) or [])
        )
        raw_edited = restore("edited_files", lambda: data.get("edited_files", {}) or {})
        if not isinstance(raw_edited, Mapping):
            raise RecoveryFailed("session field edited_files must be a mapping")
        session.edited_files = restore("edited_files", lambda: {str(k): int(v) for k, v in raw_edited.items()})
        session.read_files = set(
            restore("read_files", lambda: _strict_text_list(data.get("read_files", []) or [], "read_files"))
        )
        raw_verifications = restore("verifications", lambda: data.get("verifications", []) or [])
        raw_executed = restore("executed", lambda: data.get("executed", {}) or {})
        session.verifications, session.executed = _validate_restored_receipts(
            raw_verifications, raw_executed
        )
        session.notes_saved = restore("notes_saved", lambda: int(data.get("notes_saved", 0) or 0))
        session.transcript_notes = restore(
            "transcript_notes", lambda: _strict_text_list(data.get("transcript_notes", []) or [], "transcript_notes")
        )
        session.last_done_text = restore("last_done_text", lambda: str(data.get("last_done_text", "") or ""))
        session.turn = restore("turn", lambda: int(data.get("turn", 0) or 0))
        return session


__all__ = ["TaskSession", "turn_effect_id"]
