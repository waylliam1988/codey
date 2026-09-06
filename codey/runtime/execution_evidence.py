"""Bounded in-memory execution facts for one project task."""

from __future__ import annotations

from dataclasses import dataclass

from codey.runtime.events import RunEvent
from codey.policies.redaction import looks_prompt_visible_secret
from codey.workspace.revision import (
    INITIAL_WORKSPACE_REVISION,
    valid_workspace_fingerprint,
    valid_workspace_revision,
)

MAX_CHANGED_FILES = 32
MAX_READS = 48
MAX_SEARCHES = 32
MAX_CHECKS = 8
MAX_FAILED_CHECKS = 8
MAX_TRUNCATED_RESULTS = 16
MAX_RENDER_CHARS = 5_000
MAX_CHECK_SUMMARY_CHARS = 400
MAX_CHECK_SUMMARY_LINES = 6
NON_CHECK_RUN_ERROR_CODES = frozenset({
    "command_not_found",
    "invalid_command",
    "path_resolution_failed",
    "policy_denied",
    "timeout",
})


def _text(value: object, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def check_failure_summary(outcome: object) -> str:
    """A bounded, secret-screened tail of one failed run's model text.

    The tail carries pytest's short summary; head lines are usually the
    banner. Secret-looking lines are dropped entirely rather than masked,
    and the whole field is capped so evidence stays bounded in memory.
    """

    raw_lines = str(getattr(outcome, "model_text", "") or "").splitlines()
    kept: list[str] = []
    for line in reversed(raw_lines):
        text = line.strip()
        if not text:
            continue
        if looks_prompt_visible_secret(text):
            continue
        kept.append(text[:200])
        if len(kept) >= MAX_CHECK_SUMMARY_LINES:
            break
    summary = "\n".join(reversed(kept))
    if len(summary) > MAX_CHECK_SUMMARY_CHARS:
        summary = summary[-MAX_CHECK_SUMMARY_CHARS:].lstrip()
    return summary


def _is_non_check_run_failure(item: "CheckEvidence") -> bool:
    return item.exit_code is None and item.error_code in NON_CHECK_RUN_ERROR_CODES


@dataclass(frozen=True)
class CheckEvidence:
    """One observed verification run.

    The extra fields are bounded execution facts for repair contexts:
    ``result_summary`` is a capped, secret-screened output tail -- never
    raw stdout -- and ``managed_output_handle`` points at locally retained
    full output without carrying any of it.
    """

    command: str
    cwd: str = "."
    exit_code: int | None = None
    error_code: str = ""
    result_summary: str = ""
    managed_output_handle: str = ""
    workspace_revision: int = INITIAL_WORKSPACE_REVISION
    workspace_fingerprint: str = ""


@dataclass(frozen=True)
class ReadEvidence:
    path: str
    offset: int
    limit: int
    epoch: int
    ok: bool
    truncated: bool


@dataclass(frozen=True)
class SearchEvidence:
    tool: str
    path: str
    value: str
    epoch: int
    ok: bool
    complete: bool


@dataclass(frozen=True)
class TruncatedEvidence:
    tool: str
    detail: str
    epoch: int


class ExecutionEvidence:
    """Aggregate local tool facts without persisting source or tool output."""

    def __init__(
        self,
        *,
        workspace_revision: int = INITIAL_WORKSPACE_REVISION,
        workspace_fingerprint: str = "",
    ) -> None:
        self.workspace_revision = (
            valid_workspace_revision(workspace_revision)
            or INITIAL_WORKSPACE_REVISION
        )
        self.workspace_fingerprint = valid_workspace_fingerprint(workspace_fingerprint)
        self.edit_epoch = 0
        self.changed_files: list[str] = []
        self.reads: list[ReadEvidence] = []
        self.searches: list[SearchEvidence] = []
        self.checks_after_edit: list[CheckEvidence] = []
        self.failed_checks_after_edit: list[CheckEvidence] = []
        self.truncated_results: list[TruncatedEvidence] = []
        self.duplicate_info_tools = 0
        self.observed_tool_events = 0
        self._seen_info: set[tuple[object, ...]] = set()

    def seed_checks(self, checks: object) -> None:
        for item in checks or ():
            command = _text(getattr(item, "command", ""))
            cwd = _text(getattr(item, "cwd", "."), 240) or "."
            revision = valid_workspace_revision(
                getattr(item, "workspace_revision", 0)
            )
            fingerprint = valid_workspace_fingerprint(
                getattr(item, "workspace_fingerprint", "")
            )
            if command and revision and fingerprint:
                self._append_check(
                    self.checks_after_edit,
                    CheckEvidence(
                        command,
                        cwd,
                        workspace_revision=revision,
                        workspace_fingerprint=fingerprint,
                    ),
                )

    def set_workspace_revision(self, revision: object) -> None:
        number = valid_workspace_revision(revision)
        if number:
            self.workspace_revision = number

    def set_workspace_state(self, revision: object, fingerprint: object) -> None:
        number = valid_workspace_revision(revision)
        if number:
            self.workspace_revision = number
        self.workspace_fingerprint = valid_workspace_fingerprint(fingerprint)

    def record(self, event: RunEvent) -> None:
        if event.kind != "tool" or event.call is None or event.outcome is None:
            return
        self.observed_tool_events += 1
        call = event.call
        outcome = event.outcome
        name = call.name
        if name == "edit" and outcome.ok and outcome.changed:
            self.edit_epoch += 1
            self.checks_after_edit.clear()
            self.failed_checks_after_edit.clear()
            self._seen_info.clear()
            path = _text(call.args.get("path"), 240)
            if path and path not in self.changed_files:
                self.changed_files.append(path)
                del self.changed_files[:-MAX_CHANGED_FILES]
            return
        if name == "run":
            self._record_run(call.args, outcome)
            self._record_truncation(name, _text(call.args.get("command")), outcome.truncated)
            return
        if name == "read":
            path = _text(call.args.get("path"), 240)
            offset = self._integer(call.args.get("offset"), 1)
            limit = self._integer(call.args.get("limit"), 0)
            item = ReadEvidence(path, offset, limit, self.edit_epoch, outcome.ok, outcome.truncated)
            key = (name, path, offset, limit, self.edit_epoch)
            self._record_information(key, self.reads, item, MAX_READS)
            self._record_truncation(name, f"{path}:{offset}", outcome.truncated)
            return
        if name in {"search", "references"}:
            path = _text(call.args.get("path"), 240) or "."
            arg = "query" if name == "search" else "symbol"
            value = _text(call.args.get(arg), 240)
            item = SearchEvidence(
                name,
                path,
                value,
                self.edit_epoch,
                outcome.ok,
                outcome.ok and not outcome.truncated,
            )
            key = (name, path, value, self.edit_epoch)
            self._record_information(key, self.searches, item, MAX_SEARCHES)
            self._record_truncation(name, f"{value} under {path}", outcome.truncated)

    @property
    def successful_checks(self) -> tuple[CheckEvidence, ...]:
        return tuple(
            item
            for item in self.checks_after_edit
            if self._check_matches_workspace(item)
        )

    @property
    def failed_checks(self) -> tuple[CheckEvidence, ...]:
        return tuple(
            item
            for item in self.failed_checks_after_edit
            if self._check_matches_workspace(item)
        )

    @property
    def has_successful_checks(self) -> bool:
        return bool(self.successful_checks)

    def invalidate_checks(self) -> None:
        """Drop check facts after an out-of-band workspace change."""
        self.checks_after_edit.clear()
        self.failed_checks_after_edit.clear()

    def render_for_review(self) -> str:
        all_reads = self._unique(item.path for item in self.reads if item.ok)
        current_reads = self._unique(
            item.path for item in self.reads if item.epoch == self.edit_epoch and item.ok
        )
        all_complete_searches = self._unique(
            f"{item.tool} {item.value} under {item.path}"
            for item in self.searches
            if item.complete
        )
        complete_searches = self._unique(
            f"{item.tool} {item.value} under {item.path}"
            for item in self.searches
            if item.epoch == self.edit_epoch and item.complete
        )
        all_incomplete_searches = self._unique(
            f"{item.tool} {item.value} under {item.path}"
            for item in self.searches
            if item.ok and not item.complete
        )
        incomplete_searches = self._unique(
            f"{item.tool} {item.value} under {item.path}"
            for item in self.searches
            if item.epoch == self.edit_epoch and item.ok and not item.complete
        )
        truncated = self._unique(
            f"{item.tool} {item.detail} (epoch {item.epoch})"
            for item in self.truncated_results
        )
        lines = [
            "Execution Evidence (bounded local facts):",
            f"- Latest edit epoch: {self.edit_epoch}",
            f"- Changed files observed: {self._joined(self.changed_files)}",
            f"- Files read during task: {self._joined(all_reads)}",
            f"- Files read after latest edit: {self._joined(current_reads)}",
            f"- Complete searches during task: {self._joined(all_complete_searches)}",
            f"- Complete searches after latest edit: {self._joined(complete_searches)}",
            f"- Incomplete/truncated searches during task: {self._joined(all_incomplete_searches)}",
            f"- Incomplete/truncated searches after latest edit: {self._joined(incomplete_searches)}",
            f"- Successful checks after latest edit: {self._checks(list(self.successful_checks))}",
            f"- Failed checks after latest edit: {self._checks(list(self.failed_checks))}",
            f"- Truncated tool results during task: {self._joined(truncated)}",
            f"- Repeated identical information calls within edit epochs: {self.duplicate_info_tools}",
            "This is execution evidence, not proof of correctness or coverage.",
        ]
        rendered = "\n".join(lines)
        if len(rendered) > MAX_RENDER_CHARS:
            rendered = rendered[:MAX_RENDER_CHARS].rstrip() + "\n[evidence truncated]"
        return rendered

    def _record_run(self, args: dict, outcome: object) -> None:
        command = _text(args.get("command"))
        cwd = _text(args.get("path"), 240) or "."
        if not command:
            return
        ok = bool(
            getattr(outcome, "ok", False)
            and getattr(outcome, "exit_code", None) == 0
        )
        handle = ""
        managed = getattr(outcome, "managed_output", None)
        if callable(managed):
            try:
                handle = str((managed() or {}).get("handle") or "")[:80]
            except Exception:
                handle = ""
        item = CheckEvidence(
            command,
            cwd,
            exit_code=getattr(outcome, "exit_code", None),
            error_code=_text(getattr(outcome, "error_code", ""), 80),
            result_summary="" if ok else check_failure_summary(outcome),
            managed_output_handle=handle,
            workspace_revision=self.workspace_revision,
            workspace_fingerprint=self.workspace_fingerprint,
        )
        if ok:
            self.failed_checks_after_edit[:] = [
                existing
                for existing in self.failed_checks_after_edit
                if (existing.command, existing.cwd) != (command, cwd)
            ]
            self._append_check(self.checks_after_edit, item)
            return
        if not _is_non_check_run_failure(item):
            self.checks_after_edit.clear()
        self._append_check(self.failed_checks_after_edit, item, MAX_FAILED_CHECKS)

    def _record_information(self, key, values: list, item, limit: int) -> None:
        if key in self._seen_info:
            self.duplicate_info_tools += 1
        else:
            self._seen_info.add(key)
        values.append(item)
        del values[:-limit]

    def _record_truncation(self, tool: str, detail: str, truncated: bool) -> None:
        if not truncated:
            return
        self.truncated_results.append(TruncatedEvidence(tool, detail, self.edit_epoch))
        del self.truncated_results[:-MAX_TRUNCATED_RESULTS]

    @staticmethod
    def _append_check(values: list[CheckEvidence], item: CheckEvidence, limit: int = MAX_CHECKS) -> None:
        # Identity is the executed (command, cwd): a re-run replaces the
        # earlier observation even when exit code or output differ.
        values[:] = [
            existing
            for existing in values
            if (existing.command, existing.cwd) != (item.command, item.cwd)
        ]
        values.append(item)
        del values[:-limit]

    def _check_matches_workspace(self, item: CheckEvidence) -> bool:
        return (
            item.workspace_revision == self.workspace_revision
            and bool(self.workspace_fingerprint)
            and item.workspace_fingerprint == self.workspace_fingerprint
        )

    @staticmethod
    def _integer(value: object, default: int) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    @staticmethod
    def _unique(values) -> list[str]:
        result: list[str] = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _joined(values: list[str]) -> str:
        return ", ".join(values) if values else "(none observed)"

    @staticmethod
    def _checks(values: list[CheckEvidence]) -> str:
        if not values:
            return "(none observed)"
        return ", ".join(f"{item.command} (cwd {item.cwd})" for item in values)
