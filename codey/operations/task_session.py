"""Task session facts for the unified kernel: bounded refs, never raw output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codey.runtime.core.models import ToolResult
from codey.utils.refs import stable_ref


def session_checks_passed(session: Any, proof: Any = None) -> bool:
    """Project the common proof/facts into a receipt without a new decision."""
    if getattr(session, "edited_files", None):
        return any(row.check_id == "relevant_verification" and row.status == "pass"
                   for row in getattr(proof, "checks", ()))
    verifications = getattr(session, "verifications", ()) or ()
    if not verifications:
        return False
    latest = verifications[-1]
    return isinstance(latest, dict) and type(latest.get("passed")) is bool and latest["passed"]


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
    # Explicit per-session controller deny-list (e.g. experiment arms):
    # subtracted from the state-derived allow-list every turn, so the
    # narrowed scope flows through the SAME snapshot into prompt, schemas,
    # parsing, and execution. Empty means state-derived only.
    controller_denied: tuple[str, ...] = ()
    # Explicit completion requirement from the task entry; never inferred
    # from keywords or write permission. Read-only tasks keep False even
    # when the policy still grants project.write for other reasons.
    project_changes_required: bool = False
    coding_context_enabled: bool = True
    verification_forbidden: bool = False
    verification_candidates: tuple[Any, ...] = ()
    verification_candidate_loader: Any = field(default=None, repr=False, compare=False)
    verification_candidates_epoch: int = -1
    verification_candidates_refresh_failed: bool = False
    selected_verification: Any = None
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
    last_done_args: dict[str, Any] = field(default_factory=dict)
    turn: int = 0
    executed: dict[str, dict[str, Any]] = field(default_factory=dict)
    _memory_results: dict[str, ToolResult] = field(default_factory=dict, repr=False, compare=False)
    restored_effect_ids: set[str] = field(default_factory=set, repr=False, compare=False)

    def record_search(self, query: str) -> None:
        text = str(query or "").strip()
        if text:
            self.searches.append(text[:240])

    def record_search_result(self, result_id: str, url: str) -> None:
        rid = str(result_id or "").strip().lower()
        target = str(url or "").strip()
        if rid and target:
            self.search_results[rid] = target

    def record_open(self, url: str, *, source_id: str = "") -> None:
        text = str(url or "").strip()
        if text:
            self.opened_sources.add(text)
            sid = str(source_id or "").strip().lower()
            if not sid:
                sid = next((key for key, value in self.source_ids.items() if value == text), "")
            if not sid:
                sid = f"s{len(self.source_ids) + 1}"
            self.source_ids[sid] = text

    def record_evidence(self, source_url: str, excerpt: str) -> None:
        url = str(source_url or "").strip()
        clip = str(excerpt or "").strip()
        if url and clip:
            self.evidence.append({"source_url": url, "excerpt": clip[:600]})

    def record_edit(self, path: str, revision: int | None = None) -> int:
        key = str(path or "").strip() or "file"
        rev = revision if type(revision) is int else -1
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
        cwd: str = ".",
    ) -> None:
        if type(passed) is not bool:
            raise TypeError("verification passed must be a boolean")
        if type(revision) is not int:
            return
        rev = revision
        row: dict[str, Any] = {"command": str(command or ""), "cwd": str(cwd or "."),
                               "revision": rev, "passed": passed}
        if exit_code is not None:
            try:
                from codey.utils.refs import strict_exit_code as _strict_exit

                code = _strict_exit(exit_code)
            except Exception:
                code = None
            if code is not None:
                row["exit_code"] = code
        if type(workspace_revision) is int:
            row["workspace_revision"] = workspace_revision
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


__all__ = ["TaskSession", "turn_effect_id"]
