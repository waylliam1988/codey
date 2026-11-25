"""Deterministic test world for fault-injection scenarios.

Everything durable lives under one temporary ``state_home``. A "restart"
drops every in-memory object and reopens the same directory -- the closest
deterministic equivalent of killing the process (memory is gone, file
descriptors are gone, bytes on disk are identical). Memory-only surfaces
(event bus, approvals, worker threads) rebuild EMPTY by design; the oracle
asserts they never rebuild half-full.

No wall-clock time and no uuids ever enter a durable payload: ids come from
world counters (``op-001``, ``effect-001``) and timestamps from ``FakeClock``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codey.app.approval_registry import ApprovalRegistry
from codey.app.event_bus import EventBus
from codey.durable import DurableSession, SQLiteDurableStore
from codey.ghost.backend import GhostMemory
from codey.memory.contracts import MemoryEvent, MemoryHost
from codey.repairs.journal import RepairJournal
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    compute_args_digest,
)
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    DeliveryBatchProjection,
    ToolResultDeliveryStore,
    compute_batch_digest,
)
from codey.runtime.write.checkpoint import RuntimeOperationStore, view_from_checkpoint
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, require_json_string
from tests.stress.faults import FaultController


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
        self.received: list[dict[str, object]] = []
        self.executed: list[dict[str, object]] = []

    def send(self, request: dict[str, object]) -> dict[str, object]:
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
        # This simulation validates process-restart recovery, not power-loss
        # durability. Keep production's FULL sync default out of its stress
        # throughput path while retaining SQLite transaction semantics.
        self.durable = DurableSession(
            SQLiteDurableStore(self.state_home / "durable.sqlite3", synchronous="NORMAL")
        )
        # Runtime stores are read/write adapters over the same durable
        # checkpoint.  JSONL remains an optional audit projection and is not
        # opened by the stress world.
        self.line = RuntimeMutationLine(durable=self.durable)
        self.operations = RuntimeOperationStore(durable=self.durable)
        self.effects = RuntimeEffectStore(durable=self.durable)
        self.delivery = ToolResultDeliveryStore(durable=self.durable)
        self.ghost = GhostMemory(MemoryHost(self.state_home / "memory" / "ghost",
            lambda: (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.clock.now)).isoformat(),
            lambda: None))
        self.bus = EventBus(replay_limit=512)
        self.approvals = ApprovalRegistry()
        self.journal = RepairJournal(self.state_home)
        self.provider = FakeProvider()

    def restart(self) -> StressWorld:
        """Logical process kill: forget everything in memory, reopen disk state."""
        self.durable.close()
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
        self.durable.admit_input(
            conversation_id=self.session_id,
            request_id=f"request-{run_id}",
            task_id=run_id,
            text="stress input",
            provider_snapshot={"provider_id": provider_id, "model_id": "stress"},
            task_state={"task_kind": "stress"},
        )
        self.durable.start_task(run_id, state={"task_kind": "stress"})
        self.durable.advance_task(
            task_id=run_id,
            phase="running",
            state={"task_kind": "stress"},
        )
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
            args_digest=compute_args_digest({"provider_id": provider_id, "turn": turn}),
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
            args_digest=compute_args_digest({"tool": "read", "ref": ref}),
            display_ref=ref,
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

    def undelivered_batches(self, run_id: str) -> tuple[DeliveryBatchProjection, ...]:
        return self.delivery.undelivered_replayable_batches(self.session_id, run_id)

    # -- ghost ---------------------------------------------------------------

    def ghost_append(self, n: int = 1, *, tag: str = "op") -> list[JsonObject]:
        events: list[JsonObject] = [
            {
                "schema_version": self.GHOST_SCHEMA_VERSION,
                "type": self.GHOST_KIND,
                "id": f"ghost-{tag}-{self._ghost_seq + index + 1}",
                "ts": self.clock.tick(),
            }
            for index in range(n)
        ]
        self._ghost_seq += n
        for payload in events:
            identity = require_json_string(payload["id"], "id")
            self.ghost.observe(MemoryEvent(identity, "task.completed", self.ghost_time(),
                self.session_id, "", identity, "stress",
                {"user_text": json.dumps(payload, sort_keys=True), "assistant_text": "stress"},
                ("stress:" + identity,)))
        return events

    def ghost_time(self) -> str:
        return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self.clock.now)).isoformat()

    def ghost_rows(self) -> tuple[JsonObject, ...]:
        state = self.ghost.export_state()
        items = state["items"]
        if not isinstance(items, list):
            raise ValueError("expected memory items")
        result: list[JsonObject] = []
        for value in items:
            item = decode_json_object(value)
            experience = decode_json_object(json.loads(require_json_string(item["text"], "text")))
            result.append(decode_json_object(json.loads(require_json_string(experience["user"], "user"))))
        return tuple(result)

    # -- bus / approvals / journal ----------------------------------------------

    def sse_emit(self, event_id: str, **fields: object) -> None:
        self.bus.emit({"event_key": event_id, **fields})

    def approve(self, command: str = "echo hi", run_id: str = "run-1") -> str:
        from codey.agents.shell_approval import shell_command_payload

        self._approval_seq += 1
        approval_id = f"approval-{self._approval_seq:03d}"
        fields = shell_command_payload(command)
        self.approvals.add_shell(approval_id, {
            "id": approval_id,
            "run_id": run_id,
            "session_id": self.session_id,
            "command": command,
            "command_preview": fields["command"],
            "command_sha256": fields["command_sha256"],
            "command_chars": fields["command_chars"],
            "command_truncated": fields["command_truncated"],
            "cwd": ".",
        })
        return approval_id

    # -- canonical reading ------------------------------------------------------

    #: Payload keys that observe wall-clock time: real observations, but not
    #: convergence facts. Dropped from canonical rows (documented, not hidden).
    VOLATILE_PAYLOAD_KEYS = frozenset({"created_at", "started_at", "updated_at"})

    def _runtime_fact(self, kind: str, payload: JsonObject) -> JsonObject:
        return {
            "kind": kind,
            "operation_id": payload["operation_id"],
            "record": payload.get("record_kind", ""),
            "effect_id": payload.get("effect_id", ""),
            "ref": payload.get("ref", ""),
            "payload": {key: value for key, value in payload.items() if key not in self.VOLATILE_PAYLOAD_KEYS},
        }

    def canonical(self) -> dict[str, object]:
        rows: list[JsonObject] = []
        durable_runs: set[str] = set()
        for task in self.durable.tasks():
            if task.conversation_id != self.session_id:
                continue
            view = view_from_checkpoint(*self.durable.task_facts(task.task_id), session_id=self.session_id)
            durable_runs.add(task.task_id)
            if view.state is not None:
                payload = canonical_json_object(view.state.to_payload())
                rows.append(self._runtime_fact("operation_state", payload))
            for effect in view.effects:
                rows.append(self._runtime_fact("operation_effect", canonical_json_object(effect.intent.to_payload())))
                if effect.settlement is not None:
                    rows.append(self._runtime_fact("operation_effect", canonical_json_object(effect.settlement.to_payload())))
        ghost_rows = [
            {key: row[key] for key in sorted(row)} for row in self.ghost_rows()
        ]
        # Run ids come from durable rows, never from memory: _runs is empty
        # after a restart and must not hide committed batches.
        batches = []
        for run_id in sorted(durable_runs):
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
        batches.sort(key=lambda b: str(b["batch_id"]))
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
                if not line.strip():
                    continue
                try:
                    record = decode_json_object(json.loads(line))
                except json.JSONDecodeError:
                    # Torn tail from a mid-flush kill: the audit trail loses
                    # its tail, but recovery must not die with it (cf. the
                    # run ledger read path, which skips bad lines the same way).
                    continue
                record.pop("time", None)
                journal_events.append(record)
        return {"log_rows": rows, "ghost_rows": ghost_rows, "delivery_batches": batches,
                "approvals": approvals, "journal_events": journal_events}



__all__ = [
    "FakeClock",
    "FakeProvider",
    "FakeProviderTimeout",
    "StressWorld",
]
