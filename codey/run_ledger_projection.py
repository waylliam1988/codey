"""Read-only projections over durable run ledger facts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from codey.receipt import TaskReceipt, build_task_receipt
from codey.run_ledger import SCHEMA_VERSION, RunLedgerRecord, RunLedgerStore, read_ledger


@dataclass(frozen=True)
class ChangeFileSummary:
    path: str
    status: str
    additions: int | None
    deletions: int | None


@dataclass(frozen=True)
class ChangesSummary:
    ok: bool
    mode: str
    changed_count: int
    files: tuple[ChangeFileSummary, ...]
    files_truncated: bool
    checks_passed: bool


@dataclass(frozen=True)
class VerifiedCommandSummary:
    command: str
    cwd: str
    turn: int
    tool_id: str


@dataclass(frozen=True)
class ProviderFailureSummary:
    provider: str
    action: str
    kind: str
    stage: str
    message: str


@dataclass(frozen=True)
class ProviderSwitchSummary:
    from_provider: str
    to_provider: str
    phase: str
    reason: str


@dataclass(frozen=True)
class RunLedgerProjection:
    run_id: str = ""
    session_id: str = ""
    project: str = ""
    mode: str = ""
    started_at: str = ""
    finished_at: str = ""
    stop_reason: str = ""
    provider_initial: str = ""
    provider_final: str = ""
    task_chars: int = 0
    turns: int = 0
    max_turns: int = 0
    model_reply_count: int = 0
    model_reply_chars: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_counts: Mapping[str, int] = field(default_factory=dict)
    changed_files_observed: tuple[str, ...] = ()
    verified_commands: tuple[VerifiedCommandSummary, ...] = ()
    provider_failures: tuple[ProviderFailureSummary, ...] = ()
    provider_switches: tuple[ProviderSwitchSummary, ...] = ()
    final_changes: ChangesSummary | None = None
    ledger_truncated: bool = False
    has_run_started: bool = False
    has_run_finished: bool = False

    @property
    def complete(self) -> bool:
        return self.has_run_started and self.has_run_finished and not self.ledger_truncated


def project_run_ledger(records: Iterable[RunLedgerRecord]) -> RunLedgerProjection:
    """Build a bounded read model from ledger records.

    Unknown, future-schema, and malformed events are ignored so that projection
    readers can safely coexist with newer ledger writers.
    """

    run_id = ""
    session_id = ""
    project = ""
    mode = ""
    started_at = ""
    finished_at = ""
    stop_reason = ""
    provider_initial = ""
    provider_final = ""
    task_chars = 0
    turns = 0
    max_turns = 0
    model_reply_count = 0
    model_reply_chars = 0
    tool_calls = 0
    tool_errors = 0
    tool_counts: Counter[str] = Counter()
    changed_files: list[str] = []
    changed_file_keys: set[str] = set()
    verified_commands: list[VerifiedCommandSummary] = []
    verified_keys: set[tuple[str, str]] = set()
    provider_failures: list[ProviderFailureSummary] = []
    provider_switches: list[ProviderSwitchSummary] = []
    final_changes: ChangesSummary | None = None
    ledger_truncated = False
    has_run_started = False
    has_run_finished = False

    for payload in _sorted_payloads(records):
        event_type = _str(payload.get("type"))
        if not event_type:
            continue
        run_id = run_id or _str(payload.get("run_id"))
        session_id = session_id or _str(payload.get("session_id"))
        if event_type == "run_started":
            has_run_started = True
            started_at = started_at or _str(payload.get("ts"))
            project = _str(payload.get("project"))
            mode = _str(payload.get("mode"))
            task_chars = _int(payload.get("task_chars"))
            provider = _str(payload.get("provider"))
            if provider:
                provider_initial = provider_initial or provider
                provider_final = provider
            continue
        if event_type == "provider_selected":
            provider = _str(payload.get("provider"))
            if provider:
                provider_initial = provider_initial or provider
                provider_final = provider
            continue
        if event_type == "model_reply":
            model_reply_count += 1
            model_reply_chars += _int(payload.get("reply_chars"))
            continue
        if event_type == "tool_finished":
            tool_calls += 1
            tool = _str(payload.get("tool"))
            if tool:
                tool_counts[tool] += 1
            if payload.get("ok") is False:
                tool_errors += 1
            continue
        if event_type == "file_changed":
            path = _str(payload.get("path"))
            if path and path not in changed_file_keys:
                changed_file_keys.add(path)
                changed_files.append(path)
            continue
        if event_type == "command_verified":
            command = _str(payload.get("command"))
            cwd = _str(payload.get("cwd")) or "."
            if not command:
                continue
            key = (command, cwd)
            if key in verified_keys:
                continue
            verified_keys.add(key)
            verified_commands.append(VerifiedCommandSummary(
                command=command,
                cwd=cwd,
                turn=_int(payload.get("turn")),
                tool_id=_str(payload.get("tool_id")),
            ))
            continue
        if event_type == "changes_collected":
            final_changes = _changes_summary(payload)
            continue
        if event_type == "provider_failure":
            provider_failures.append(ProviderFailureSummary(
                provider=_str(payload.get("provider")),
                action=_str(payload.get("action")),
                kind=_str(payload.get("kind")),
                stage=_str(payload.get("stage")),
                message=_str(payload.get("message")),
            ))
            continue
        if event_type == "provider_switched":
            to_provider = _str(payload.get("to_provider"))
            provider_switches.append(ProviderSwitchSummary(
                from_provider=_str(payload.get("from_provider")),
                to_provider=to_provider,
                phase=_str(payload.get("phase")),
                reason=_str(payload.get("reason")),
            ))
            if to_provider:
                provider_final = to_provider
            continue
        if event_type == "ledger_truncated":
            ledger_truncated = True
            continue
        if event_type == "run_finished":
            has_run_finished = True
            finished_at = _str(payload.get("ts"))
            stop_reason = _str(payload.get("stop_reason"))
            turns = _int(payload.get("turns"))
            max_turns = _int(payload.get("max_turns"))
            provider = _str(payload.get("provider"))
            if provider:
                provider_final = provider

    return RunLedgerProjection(
        run_id=run_id,
        session_id=session_id,
        project=project,
        mode=mode,
        started_at=started_at,
        finished_at=finished_at,
        stop_reason=stop_reason,
        provider_initial=provider_initial,
        provider_final=provider_final,
        task_chars=task_chars,
        turns=turns,
        max_turns=max_turns,
        model_reply_count=model_reply_count,
        model_reply_chars=model_reply_chars,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        tool_counts=dict(sorted(tool_counts.items())),
        changed_files_observed=tuple(changed_files),
        verified_commands=tuple(verified_commands),
        provider_failures=tuple(provider_failures),
        provider_switches=tuple(provider_switches),
        final_changes=final_changes,
        ledger_truncated=ledger_truncated,
        has_run_started=has_run_started,
        has_run_finished=has_run_finished,
    )


def load_run_projection(
    store: RunLedgerStore | None,
    session_id: str,
    run_id: str,
) -> RunLedgerProjection | None:
    if store is None:
        return None
    try:
        records = read_ledger(store.path_for(session_id, run_id))
    except Exception:
        return None
    if not records:
        return None
    return project_run_ledger(records)


def build_task_receipt_from_projection(
    projection: RunLedgerProjection,
) -> TaskReceipt | None:
    changes = projection.final_changes
    if changes is None:
        return None
    return build_task_receipt(
        {
            "changed_count": changes.changed_count,
            "mode": changes.mode,
        },
        checks_passed=changes.checks_passed,
    )


def receipt_from_projection_if_compatible(
    projection: RunLedgerProjection | None,
    legacy_receipt: object,
) -> TaskReceipt | None:
    if projection is None or not projection.complete or projection.ledger_truncated:
        return None
    projected = build_task_receipt_from_projection(projection)
    if projected is None or not isinstance(legacy_receipt, dict):
        return None
    if (
        projected.changed_count == _int(legacy_receipt.get("changed_count"))
        and projected.restore_available == _bool(legacy_receipt.get("restore_available"))
        and projected.checks_passed == _bool(legacy_receipt.get("checks_passed"))
    ):
        return projected
    return None


def _sorted_payloads(records: Iterable[RunLedgerRecord]) -> list[dict[str, object]]:
    payloads: list[tuple[int, int, dict[str, object]]] = []
    for index, record in enumerate(records):
        payload = record.payload if isinstance(record, RunLedgerRecord) else None
        if not isinstance(payload, dict):
            continue
        if payload.get("schema_version") != SCHEMA_VERSION:
            continue
        payloads.append((_int(payload.get("seq"), default=index), index, payload))
    return [payload for _seq, _index, payload in sorted(payloads, key=lambda item: (item[0], item[1]))]


def _changes_summary(payload: dict[str, object]) -> ChangesSummary:
    files: list[ChangeFileSummary] = []
    source_files = payload.get("files")
    if isinstance(source_files, list):
        for item in source_files:
            if not isinstance(item, dict):
                continue
            path = _str(item.get("path"))
            if not path:
                continue
            files.append(ChangeFileSummary(
                path=path,
                status=_str(item.get("status")),
                additions=_optional_int(item.get("additions")),
                deletions=_optional_int(item.get("deletions")),
            ))
    return ChangesSummary(
        ok=payload.get("ok") is not False,
        mode=_str(payload.get("mode")),
        changed_count=max(0, _int(payload.get("changed_count"))),
        files=tuple(files),
        files_truncated=_bool(payload.get("files_truncated")),
        checks_passed=_bool(payload.get("checks_passed")),
    )


def _str(value: object) -> str:
    return str(value or "")


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: object) -> bool:
    return value is True
