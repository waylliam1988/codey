"""Task session facts for the unified kernel: bounded refs, never raw output."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from threading import RLock

from codey.completion.contract import CompletionProof
from codey.completion.verification_policy import VerificationCandidate
from codey.policies.command_line import ShellSpec, resolve_shell
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.execution_record import ExecutionRecord, HitTarget
from codey.runtime.core.models import ToolResult
from codey.runtime.core.tool_dispatch import ToolDispatch
from codey.runtime.core.verification_observation import VerificationRecord
from codey.types.json_value import JsonObject
from codey.utils.refs import stable_ref


def session_checks_passed(session: TaskSession, proof: CompletionProof | None = None) -> bool:
    """Project the common proof/facts into a receipt without a new decision."""
    if session.edited_files:
        return any(row.check_id == "relevant_verification" and row.status == "pass"
                   for row in (proof.checks if proof is not None else ()))
    verifications = session.verifications
    if not verifications:
        return False
    latest = verifications[-1]
    return isinstance(latest, dict) and type(latest.get("passed")) is bool and latest["passed"]


def effect_coordinates(turn: object, tool_index: object) -> tuple[int, int]:
    """Validate identity coordinates before intent, replay or settlement."""
    if type(turn) is not int or turn < 0:
        raise ValueError("turn must be a nonnegative integer")
    if type(tool_index) is not int or tool_index < 0:
        raise ValueError("tool_index must be a nonnegative integer")
    return turn, tool_index


def turn_effect_id(run_id: object, turn: object, tool_index: object) -> str:
    """Call identity for intents: run + turn + index, never tool name+args.

    The same file may be read twice and the same command may re-run after an
    edit; those are new calls. Only the same turn slot reuses an identity.
    """

    turn, tool_index = effect_coordinates(turn, tool_index)
    return stable_ref("task_turn_effect", str(run_id or ""), turn, tool_index)


@dataclass
class TaskSession:
    policy: TaskPolicy
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
    # Optional direct model exposure. Authorization and nested capabilities
    # still come from the same policy/controller intersection.
    model_tool_names: tuple[str, ...] | None = None
    # Per-invocation source bounds. Consumption is reconstructed from ordinary
    # tool receipts, never a separate research progress ledger.
    research_acquisition_limits: tuple[int, int] | None = None
    research_acquisition_slots: dict[tuple[int, int], str] = field(default_factory=dict, repr=False)
    # Explicit completion requirement from the task entry; never inferred
    # from keywords or write permission. Read-only tasks keep False even
    # when the policy still grants project.write for other reasons.
    project_changes_required: bool = False
    coding_context_enabled: bool = True
    verification_forbidden: bool = False
    verification_candidates: tuple[VerificationCandidate, ...] = ()
    verification_candidate_loader: Callable[[], tuple[VerificationCandidate, ...]] | None = field(
        default=None, repr=False, compare=False)
    verification_candidates_epoch: int = -1
    verification_candidates_refresh_failed: bool = False
    selected_verification: VerificationCandidate | None = None
    # Current workspace identity observed by real edit/run execution.
    # Verifications carry the identity they observed; only a verification
    # whose fingerprint matches the current workspace can complete.
    workspace_revision: int = 0
    workspace_fingerprint: str = ""
    searches: list[str] = field(default_factory=list)
    research_questions: tuple[str, ...] = ()
    search_results: dict[str, str] = field(default_factory=dict)
    opened_sources: set[str] = field(default_factory=set)
    source_ids: dict[str, str] = field(default_factory=dict)
    hit_targets: dict[str, HitTarget] = field(default_factory=dict)
    evidence: list[dict[str, str]] = field(default_factory=list)
    edited_files: dict[str, int] = field(default_factory=dict)
    read_files: set[str] = field(default_factory=set)
    verifications: list[VerificationRecord] = field(default_factory=list)
    notes_saved: int = 0
    transcript_notes: list[str] = field(default_factory=list)
    last_done_text: str = ""
    last_done_args: JsonObject = field(default_factory=dict)
    turn: int = 0
    executed: dict[str, ExecutionRecord] = field(default_factory=dict)
    _memory_results: dict[str, ToolResult] = field(default_factory=dict, repr=False, compare=False)
    restored_effect_ids: set[str] = field(default_factory=set, repr=False, compare=False)
    shell: ShellSpec = field(default_factory=resolve_shell)
    projection_lock: RLock = field(default_factory=RLock, repr=False, compare=False)
    tool_dispatch: ToolDispatch = field(default_factory=ToolDispatch, repr=False, compare=False)

    def clone_projection(self) -> TaskSession:
        """Isolate mutable projection containers, without copying capabilities."""
        return replace(
            self,
            research_acquisition_slots=dict(self.research_acquisition_slots),
            searches=list(self.searches), search_results=dict(self.search_results),
            opened_sources=set(self.opened_sources), source_ids=dict(self.source_ids),
            hit_targets=deepcopy(self.hit_targets), evidence=deepcopy(self.evidence),
            edited_files=dict(self.edited_files), read_files=set(self.read_files),
            verifications=deepcopy(self.verifications), transcript_notes=list(self.transcript_notes),
            last_done_args=deepcopy(self.last_done_args), executed=deepcopy(self.executed),
            _memory_results=dict(self._memory_results), restored_effect_ids=set(self.restored_effect_ids),
        )

    def commit_projection(self, staged: TaskSession) -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(staged, item.name))

    def record_search(self, query: str) -> None:
        text = (query or "").strip()
        if text:
            self.searches.append(text[:240])

    def record_search_result(self, result_id: str, url: str) -> None:
        rid = (result_id or "").strip().lower()
        target = (url or "").strip()
        if rid and target:
            self.search_results[rid] = target

    def record_open(self, url: str, *, source_id: str = "") -> None:
        text = (url or "").strip()
        if text:
            sid = (source_id or "").strip().lower()
            if not sid:
                sid = self.source_id_for(text)
            if sid in self.source_ids and self.source_ids[sid] != text:
                raise ValueError(f"conflicting source identity: {sid}")
            if any(key != sid and value == text for key, value in self.source_ids.items()):
                raise ValueError(f"conflicting source identity for URL: {text}")
            self.opened_sources.add(text)
            self.source_ids[sid] = text
            self.source_ids = dict(sorted(self.source_ids.items(), key=lambda item: int(item[0][1:])))

    def source_id_for(self, url: str) -> str:
        existing = next((key for key, value in self.source_ids.items() if value == url), "")
        if existing:
            return existing
        # Explicit receipt bindings can arrive out of order during recovery.
        number = max((int(key[1:]) for key in self.source_ids if key[1:].isdigit()), default=0) + 1
        return f"s{number}"

    def record_evidence(self, source_url: str, excerpt: str) -> None:
        url = (source_url or "").strip()
        clip = (excerpt or "").strip()
        if url and clip:
            self.evidence.append({"source_url": url, "excerpt": clip[:600]})

    def record_edit(self, path: str, revision: int | None = None) -> int:
        key = (path or "").strip() or "file"
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
        row = VerificationRecord(command=command or "", cwd=cwd or ".", revision=rev, passed=passed)
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
            row["workspace_fingerprint"] = (workspace_fingerprint or "")[:120]
        self.verifications.append(row)

    def set_workspace_state(self, revision: object, fingerprint: object) -> None:
        from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision

        self.workspace_revision = valid_workspace_revision(revision)
        self.workspace_fingerprint = valid_workspace_fingerprint(fingerprint)

    def notes_text(self) -> str:
        parts = [*self.transcript_notes]
        if self.last_done_text:
            parts.append(self.last_done_text)
        return "\n".join(parts)


__all__ = ["TaskSession", "effect_coordinates", "turn_effect_id"]
