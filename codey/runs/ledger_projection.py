"""Read-only projections over durable run ledger facts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from codey.runs.ledger import SCHEMA_VERSION, RunLedgerRecord, RunLedgerStore, read_ledger
from codey.runs.receipt import TaskReceipt, task_receipt_from_payload


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
    # The schema-v1 receipt as it was durably recorded with this change
    # collection. Old ledgers have no valid receipt row here and project
    # to None; readers show their own facts instead of guessing.
    receipt: TaskReceipt | None = None


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


@dataclass
class _LedgerBuildState:
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
    tool_counts: Counter[str] = field(default_factory=Counter)
    changed_files: list[str] = field(default_factory=list)
    changed_file_keys: set[str] = field(default_factory=set)
    verified_commands: list[VerifiedCommandSummary] = field(default_factory=list)
    verified_keys: set[tuple[str, str]] = field(default_factory=set)
    provider_failures: list[ProviderFailureSummary] = field(default_factory=list)
    provider_switches: list[ProviderSwitchSummary] = field(default_factory=list)
    final_changes: ChangesSummary | None = None
    ledger_truncated: bool = False
    has_run_started: bool = False
    has_run_finished: bool = False


def _track_ledger_provider(state: _LedgerBuildState, provider: str) -> None:
    if provider:
        state.provider_initial = state.provider_initial or provider
        state.provider_final = provider


def _apply_run_started(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    state.has_run_started = True
    state.started_at = state.started_at or _str(payload.get("ts"))
    state.project = _str(payload.get("project"))
    state.mode = _str(payload.get("mode"))
    state.task_chars = _int(payload.get("task_chars"))
    _track_ledger_provider(state, _str(payload.get("provider")))


def _apply_tool_finished(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    state.tool_calls += 1
    tool = _str(payload.get("tool"))
    if tool:
        state.tool_counts[tool] += 1
    if payload.get("ok") is False:
        state.tool_errors += 1


def _apply_file_changed(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    path = _str(payload.get("path"))
    if path and path not in state.changed_file_keys:
        state.changed_file_keys.add(path)
        state.changed_files.append(path)


def _apply_command_verified(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    command = _str(payload.get("command"))
    cwd = _str(payload.get("cwd")) or "."
    if not command:
        return
    key = (command, cwd)
    if key in state.verified_keys:
        return
    state.verified_keys.add(key)
    state.verified_commands.append(VerifiedCommandSummary(
        command=command,
        cwd=cwd,
        turn=_int(payload.get("turn")),
        tool_id=_str(payload.get("tool_id")),
    ))


def _apply_provider_failure(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    state.provider_failures.append(ProviderFailureSummary(
        provider=_str(payload.get("provider")),
        action=_str(payload.get("action")),
        kind=_str(payload.get("kind")),
        stage=_str(payload.get("stage")),
        message=_str(payload.get("message")),
    ))


def _apply_provider_switched(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    to_provider = _str(payload.get("to_provider"))
    state.provider_switches.append(ProviderSwitchSummary(
        from_provider=_str(payload.get("from_provider")),
        to_provider=to_provider,
        phase=_str(payload.get("phase")),
        reason=_str(payload.get("reason")),
    ))
    if to_provider:
        state.provider_final = to_provider


def _apply_run_finished(state: _LedgerBuildState, payload: dict[str, object]) -> None:
    state.has_run_finished = True
    state.finished_at = _str(payload.get("ts"))
    state.stop_reason = _str(payload.get("stop_reason"))
    state.turns = _int(payload.get("turns"))
    state.max_turns = _int(payload.get("max_turns"))
    provider = _str(payload.get("provider"))
    if provider:
        state.provider_final = provider


def _build_projection(state: _LedgerBuildState) -> RunLedgerProjection:
    return RunLedgerProjection(
        run_id=state.run_id,
        session_id=state.session_id,
        project=state.project,
        mode=state.mode,
        started_at=state.started_at,
        finished_at=state.finished_at,
        stop_reason=state.stop_reason,
        provider_initial=state.provider_initial,
        provider_final=state.provider_final,
        task_chars=state.task_chars,
        turns=state.turns,
        max_turns=state.max_turns,
        model_reply_count=state.model_reply_count,
        model_reply_chars=state.model_reply_chars,
        tool_calls=state.tool_calls,
        tool_errors=state.tool_errors,
        tool_counts=dict(sorted(state.tool_counts.items())),
        changed_files_observed=tuple(state.changed_files),
        verified_commands=tuple(state.verified_commands),
        provider_failures=tuple(state.provider_failures),
        provider_switches=tuple(state.provider_switches),
        final_changes=state.final_changes,
        ledger_truncated=state.ledger_truncated,
        has_run_started=state.has_run_started,
        has_run_finished=state.has_run_finished,
    )


def project_run_ledger(records: Iterable[RunLedgerRecord]) -> RunLedgerProjection:
    """Build a bounded read model from ledger records.

    Unknown, future-schema, and malformed events are ignored so that projection
    readers can safely coexist with newer ledger writers.
    """

    state = _LedgerBuildState()

    for payload in _sorted_payloads(records):
        event_type = _str(payload.get("type"))
        if not event_type:
            continue
        state.run_id = state.run_id or _str(payload.get("run_id"))
        state.session_id = state.session_id or _str(payload.get("session_id"))
        if event_type == "run_started":
            _apply_run_started(state, payload)
            continue
        if event_type == "provider_selected":
            _track_ledger_provider(state, _str(payload.get("provider")))
            continue
        if event_type == "model_reply":
            state.model_reply_count += 1
            state.model_reply_chars += _int(payload.get("reply_chars"))
            continue
        if event_type == "tool_finished":
            _apply_tool_finished(state, payload)
            continue
        if event_type == "file_changed":
            _apply_file_changed(state, payload)
            continue
        if event_type == "command_verified":
            _apply_command_verified(state, payload)
            continue
        if event_type == "changes_collected":
            state.final_changes = _changes_summary(payload)
            continue
        if event_type == "provider_failure":
            _apply_provider_failure(state, payload)
            continue
        if event_type == "provider_switched":
            _apply_provider_switched(state, payload)
            continue
        if event_type == "ledger_truncated":
            state.ledger_truncated = True
            continue
        if event_type == "run_finished":
            _apply_run_finished(state, payload)

    return _build_projection(state)


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
    projection: RunLedgerProjection | None,
) -> TaskReceipt | None:
    """The durably recorded receipt of one run, or None when never recorded."""

    if projection is None or projection.final_changes is None:
        return None
    return projection.final_changes.receipt


def event_with_projected_receipt(
    store: RunLedgerStore | None,
    event: dict[str, object],
    *,
    session_id: str,
    run_id: str,
) -> dict[str, object]:
    """Attach the durable receipt projection to a terminal event when present."""

    receipt = build_task_receipt_from_projection(
        load_run_projection(store, session_id, run_id)
    )
    if receipt is None:
        return event
    updated = dict(event)
    updated["receipt"] = receipt.to_dict()
    return updated


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
        receipt=task_receipt_from_payload(payload.get("receipt")),
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
