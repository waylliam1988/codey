"""Deterministic test world for fault-injection scenarios.

Everything durable lives under one temporary ``state_home``. A "restart"
drops every in-memory object and reopens the same directory -- the closest
deterministic equivalent of killing the process (memory is gone, file
descriptors are gone, bytes on disk are identical). Memory-only surfaces
(event bus, approvals, worker threads) rebuild EMPTY by design; the oracle
asserts they never rebuild half-full.

No wall-clock time and no uuids ever enter a durable payload: ids come from
world counters (``op-001``, ``effect-001``) and timestamps from ``FakeClock``.
Mutation batch uuids assigned inside ``RuntimeSessionLog`` are normalized
away by ``model.normalize``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codey.app.approval_registry import ApprovalRegistry
from codey.app.event_bus import EventBus
from codey.ghost.event_log import GhostEventLog
from codey.repairs.journal import RepairJournal
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
)
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
)
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from tests.stress.faults import FaultController
from tests.stress.model import DurableSnapshot


class FakeClock:
    """Logical clock: every timestamp is a deterministic tick."""

    def __init__(self) -> None:
        self.now = 0

    def tick(self) -> int:
        self.now += 1
        return self.now


class FakeProviderTimeout(TimeoutError):
    """Timeout raised instead of a provider reply (fault injection)."""


class FakeProvider:
    """Provider double with explicit timeout modes.

    - ``normal``: records the request and replies.
    - ``execute_then_timeout``: records AND executes, then raises.
      The durable side must treat this as UNKNOWN, never success.
    - ``timeout_before_execute``: raises before touching anything.
    """

    NORMAL = "normal"
    EXECUTE_THEN_TIMEOUT = "execute_then_timeout"
    TIMEOUT_BEFORE_EXECUTE = "timeout_before_execute"

    def __init__(self, mode: str = NORMAL) -> None:
        self.mode = mode
        self.received: list[dict] = []
        self.executed: list[dict] = []

    def send(self, request: dict) -> dict:
        if self.mode == self.TIMEOUT_BEFORE_EXECUTE:
            raise FakeProviderTimeout("provider timeout before execute")
        self.received.append(dict(request))
        self.executed.append(dict(request))
        if self.mode == self.EXECUTE_THEN_TIMEOUT:
            raise FakeProviderTimeout("provider timeout after execute")
        return {"reply": f"reply-for-{request.get('id', '')}"}


class StressWorld:
    GHOST_SCHEMA_VERSION = 1
    GHOST_KIND = "stress_event"

    def __init__(self, state_home: str | Path, seed: int = 0) -> None:
        self.state_home = Path(state_home)
        self.session_id = "stress-session"
        self.faults = FaultController(seed)
        self.clock = FakeClock()
        self._op_seq = 0
        self._effect_seq = 0
        self._batch_seq = 0
        self._ghost_seq = 0
        self._approval_seq = 0
        self._open()

    # -- lifecycle -----------------------------------------------------

    def _open(self) -> None:
        self.log = RuntimeSessionLog(self.state_home)
        self.line = RuntimeMutationLine(self.log)
        self.operations = RuntimeOperationStore(self.log)
        self.effects = RuntimeEffectStore(self.log)
        self.delivery = ToolResultDeliveryStore(self.log)
        self.ghost = GhostEventLog(
            self.state_home / "ghost" / "stress.jsonl",
            schema_version=self.GHOST_SCHEMA_VERSION,
            source_name="stress.jsonl",
            allowed_event_kinds=(self.GHOST_KIND,),
            bad_row_policy="warn",
        )
        self.bus = EventBus(replay_limit=512)
        self.approvals = ApprovalRegistry()
        self.journal = RepairJournal(self.state_home)
        self.provider = FakeProvider()

    def restart(self) -> StressWorld:
        """Logical process kill: forget everything in memory, reopen disk state."""
        self._open()
        self.provider = FakeProvider(self.provider.mode)
        return self

    # -- deterministic ids ----------------------------------------------

    def next_op_id(self) -> str:
        self._op_seq += 1
        return f"op-{self._op_seq:03d}"

    def next_effect_id(self, category: str) -> str:
        self._effect_seq += 1
        short = "prov" if category == EFFECT_CATEGORY_PROVIDER_SEND else "tool"
        return f"effect-{short}-{self._effect_seq:03d}"

    def next_batch_id(self, run_id: str, turn: int) -> str:
        self._batch_seq += 1
        return f"batch-{run_id}-t{turn}-{self._batch_seq:03d}"

    # -- operation envelope ----------------------------------------------

    def accept_operation(self, run_id: str, provider_id: str = "mock") -> None:
        self.line.accept_operation(
            session_id=self.session_id,
            run_id=run_id,
            project="stress-project",
            provider_id=provider_id,
            turn_budget=10,
            max_repair_rounds=1,
        )
        self.line.mark_writer_running(
            session_id=self.session_id, run_id=run_id, provider_id=provider_id
        )

    # -- provider effects --------------------------------------------------

    def provider_intent(
        self, run_id: str, *, turn: int = 1, provider_id: str = "mock"
    ) -> RuntimeEffectIntent:
        return RuntimeEffectIntent(
            effect_id=self.next_effect_id(EFFECT_CATEGORY_PROVIDER_SEND),
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=self.session_id,
            run_id=run_id,
            turn=turn,
            provider_id=provider_id,
        )

    def begin_provider(
        self, run_id: str, intent: RuntimeEffectIntent, *, delivery_batch_id: str = ""
    ) -> RuntimeEffectIntent:
        return self.line.begin_provider_effect(
            session_id=self.session_id,
            run_id=run_id,
            intent=intent,
            delivery_batch_id=delivery_batch_id,
        )

    def settle_provider(
        self, run_id: str, effect_id: str, *, status: str = "ok", error_code: str = ""
    ) -> RuntimeEffectSettlement:
        return self.line.settle_provider_effect(
            session_id=self.session_id,
            run_id=run_id,
            settlement=RuntimeEffectSettlement(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=run_id,
                status=status,
                error_code=error_code,
            ),
        )

    def pending_provider_ids(self, run_id: str) -> tuple[str, ...]:
        return tuple(
            e.intent.effect_id
            for e in self.effects.pending_effects(self.session_id, run_id)
            if e.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND
        )

    # -- tool batches + delivery --------------------------------------------

    def tool_batch_intent(
        self, run_id: str, turn: int, refs: tuple[str, ...]
    ) -> DeliveryBatchIntent:
        items = tuple(
            DeliveryBatchItem(tool_index=index, tool_name="read", ref=ref, replay_class="safe")
            for index, ref in enumerate(refs)
        )
        return DeliveryBatchIntent(
            batch_id=self.next_batch_id(run_id, turn),
            session_id=self.session_id,
            run_id=run_id,
            turn=turn,
            items=items,
            batch_digest=compute_batch_digest(items),
        )

    def tool_intent(
        self, run_id: str, ref: str, *, turn: int = 1
    ) -> RuntimeEffectIntent:
        return RuntimeEffectIntent(
            effect_id=self.next_effect_id(EFFECT_CATEGORY_TOOL_CALL),
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=run_id,
            phase="writer",
            turn=turn,
            tool_index=0,
            tool_name="read",
            replay_class="safe",
        )

    def begin_batch(
        self, run_id: str, batch: DeliveryBatchIntent, tool: RuntimeEffectIntent
    ) -> None:
        self.line.begin_tool_batch(
            session_id=self.session_id,
            run_id=run_id,
            intents=(tool,),
            delivery_intent=batch,
        )

    def undelivered_batches(self, run_id: str) -> tuple:
        return self.delivery.undelivered_replayable_batches(self.session_id, run_id)

    # -- ghost ---------------------------------------------------------------

    def ghost_append(self, n: int = 1, *, tag: str = "op") -> list[dict]:
        events = [
            {
                "schema_version": self.GHOST_SCHEMA_VERSION,
                "type": self.GHOST_KIND,
                "id": f"ghost-{tag}-{self._ghost_seq + index + 1}",
                "ts": self.clock.tick(),
            }
            for index in range(n)
        ]
        self._ghost_seq += n
        assert self.ghost.append(events)
        return events

    def ghost_rows(self) -> tuple:
        return tuple(self.ghost.read().rows)

    # -- bus / approvals / journal ----------------------------------------------

    def sse_emit(self, event_id: str, **fields: Any) -> None:
        self.bus.emit({"event_key": event_id, **fields})

    def approve(self, command: str = "echo hi", run_id: str = "run-1") -> str:
        self._approval_seq += 1
        approval_id = f"approval-{self._approval_seq:03d}"
        self.approvals.add_shell(approval_id, {
            "id": approval_id,
            "run_id": run_id,
            "session_id": self.session_id,
            "command": command,
            "cwd": ".",
        })
        return approval_id

    # -- canonical reading ------------------------------------------------------

    #: Payload keys that observe wall-clock time: real observations, but not
    #: convergence facts. Dropped from canonical rows (documented, not hidden).
    VOLATILE_PAYLOAD_KEYS = frozenset({"created_at", "started_at", "updated_at"})

    def canonical(self) -> dict:
        rows = []
        batch_aliases: dict[str, str] = {}
        for entry in self.log.read(self.session_id):
            batch_id = entry.batch_id
            if batch_id not in batch_aliases:
                batch_aliases[batch_id] = f"<batch{len(batch_aliases)}>"
            payload = {
                key: json.loads(json.dumps(value, sort_keys=True, default=str))
                for key, value in entry.payload.items()
                if key not in self.VOLATILE_PAYLOAD_KEYS
            }
            rows.append({
                "session_id": entry.session_id,
                "lane": entry.lane,
                "operation_id": entry.operation_id,
                "kind": entry.kind,
                "record": payload.get("record_kind", ""),
                "effect_id": payload.get("effect_id", ""),
                "batch_id": payload.get("batch_id", ""),
                "ref": payload.get("ref", ""),
                "payload": payload,
                "batch_index": entry.batch_index,
                "batch_count": entry.batch_count,
                "batch_ref": batch_aliases[batch_id],
            })
        ghost_rows = [
            {key: row[key] for key in sorted(row)} for row in self.ghost_rows()
        ]
        # Run ids come from durable rows, never from memory: _runs is empty
        # after a restart and must not hide committed batches.
        durable_runs = sorted({
            str(row.get("payload", {}).get("run_id") or "")
            for row in rows
        } - {""})
        batches = []
        for run_id in durable_runs:
            undelivered = {
                b.intent.batch_id
                for b in self.delivery.undelivered_replayable_batches(self.session_id, run_id)
            }
            for b in self.delivery.load_batches(self.session_id, run_id):
                batches.append({
                    "batch_id": b.intent.batch_id,
                    "turn": b.intent.turn,
                    "refs": list(b.intent.tool_refs),
                    "digest": b.intent.batch_digest,
                    "delivered": b.intent.batch_id not in undelivered,
                })
        batches.sort(key=lambda b: b["batch_id"])
        approvals = sorted(
            {
                approval_id: {
                    "command": record.get("command", ""),
                    "cwd": record.get("cwd", ""),
                }
                for approval_id, record in self.approvals.shell_snapshot().items()
            }.items()
        )
        journal_events = []
        journal_path = self.journal.path
        if journal_path is not None and journal_path.exists():
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    record.pop("time", None)
                    journal_events.append(record)
        return DurableSnapshot(
            log_rows=tuple(rows),
            ghost_rows=tuple(ghost_rows),
            delivery_batches=tuple(batches),
            approvals=tuple(approvals),
            journal_events=tuple(journal_events),
        ).to_facts()


__all__ = [
    "FakeClock",
    "FakeProvider",
    "FakeProviderTimeout",
    "StressWorld",
]
