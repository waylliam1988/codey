"""CodeMode control-flow recovery metadata in the original durable checkpoint.

Child requests and results belong only to original effect intents and receipts.
State operations reference original checkpoint revisions instead of copying values.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from threading import RLock
from typing import TypedDict, cast

from codey.codemode.state import DurableCodeModeState
from codey.durable.model import TaskCheckpoint, thaw_json
from codey.durable.session import DurableSession
from codey.runtime.core.models import ToolCall


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class StateReference(TypedDict):
    request: str
    revision: int


class ReplayRecord(TypedDict):
    schema: int
    fingerprint: str
    epoch_ms: int
    seed: int
    effect_scope: str
    allow_writes: bool
    state: list[StateReference]


class CodeModeReplayError(ValueError):
    pass


class DurableCodeModeReplay:
    def __init__(
        self,
        durable: DurableSession,
        task_id: str,
        execution_id: str,
        fingerprint: str,
        *,
        parent_effect_id: str,
        effect_scope: str = "",
        allow_writes: bool = False,
    ) -> None:
        self.durable, self.task_id, self.execution_id = durable, task_id, execution_id
        self.fingerprint = fingerprint
        self.effect_scope = effect_scope
        self.parent_effect_id = parent_effect_id
        self.state = DurableCodeModeState(durable, task_id)
        self._state_index = 0
        self._children_seen: set[str] = set()
        self._lock = RLock()
        self.failure = ""

        def initialize(current: TaskCheckpoint) -> Mapping[str, object] | None:
            records = self._records(current)
            if execution_id in records:
                self._record(current)
                return None
            records[execution_id] = {
                "schema": 1,
                "fingerprint": fingerprint,
                "epoch_ms": int(time.time() * 1000),
                "effect_scope": effect_scope,
                "allow_writes": allow_writes,
                "seed": secrets.randbits(32),
                "state": [],
            }
            return {"codemode_replay": records}

        durable.transform_task_state(task_id, initialize)
        record = self._record(durable.task(task_id))
        self.context = {"epochMs": record["epoch_ms"], "seed": record["seed"]}

    @staticmethod
    def _records(current: TaskCheckpoint) -> dict[str, ReplayRecord]:
        raw = current.state.get("codemode_replay", {})
        if not isinstance(raw, Mapping):
            raise CodeModeReplayError("invalid CodeMode recovery metadata")
        return cast(dict[str, ReplayRecord], thaw_json(raw))

    def _record(self, current: TaskCheckpoint) -> ReplayRecord:
        record = self._records(current)[self.execution_id]
        if (
            not isinstance(record, dict)
            or record.get("fingerprint") != self.fingerprint
            or type(record.get("epoch_ms")) is not int
            or type(record.get("seed")) is not int
            or record.get("schema") != 1
            or type(record.get("state")) is not list
            or record.get("effect_scope") != self.effect_scope
            or type(record.get("allow_writes")) is not bool
        ):
            raise CodeModeReplayError("CodeMode recovery identity or metadata mismatch")
        return record

    def child(self, call: ToolCall, effect_id: str) -> None:
        """Inspect the original effect intent; never write a second child log."""
        try:
            self._children_seen.add(effect_id)
            try:
                effect = self.durable.effect(effect_id)
            except KeyError:
                # No original intent means dispatch has not started: execute_turn
                # will commit the one authoritative intent before running it.
                return
            if (
                effect.intent.get("parent_effect_id") != self.parent_effect_id
                or effect.intent.get("call_id") != call.call_id
            ):
                raise CodeModeReplayError("CodeMode child identity changed during recovery")
            if effect.settlement is None or effect.status not in ("ok", "error"):
                raise CodeModeReplayError("CodeMode child receipt is missing or uncertain; refusing re-execution")
        except Exception as exc:
            self.failure = str(exc)
            raise

    def store(self, key: str, value: object) -> None:
        self._state_step("store", key, value)

    def load(self, key: str) -> object:
        return self._state_step("load", key)

    def _state_step(self, op: str, key: str, value: object = None) -> object:
        try:
            with self._lock:
                key = self.state._key(key)
                request = digest({"op": op, "key": key, **({"value": value} if op == "store" else {})})
                index = self._state_index
                result: object = None

                def apply(current: TaskCheckpoint) -> Mapping[str, object] | None:
                    nonlocal result
                    records = self._records(current)
                    steps = self._record(current)["state"]
                    if index < len(steps):
                        step = steps[index]
                        if step["request"] != request:
                            raise CodeModeReplayError("CodeMode state operation changed during recovery")
                        if op == "load":
                            history = self.durable.task_timeline(self.task_id)
                            past = next((t.task for t in history if t.task.revision == step["revision"]), None)
                            if past is None:
                                raise CodeModeReplayError("CodeMode state history is missing")
                            result = self.state.values_at(past).get(key)
                        return None
                    if index != len(steps) or len(steps) >= 256:
                        raise CodeModeReplayError("CodeMode state recovery sequence exceeds its budget")
                    state = self.state.values_at(current)
                    if op == "store":
                        state = self.state.prepare_store(current, key, value)
                    else:
                        result = state.get(key)
                    steps.append({"request": request, "revision": current.revision + 1})
                    records[self.execution_id]["state"] = steps
                    patch: dict[str, object] = {"codemode_replay": records}
                    if op == "store":
                        patch["codemode_state"] = state
                    return patch

                self.durable.transform_task_state(self.task_id, apply)
                self._state_index += 1
                return result
        except Exception as exc:
            self.failure = str(exc)
            raise

    def assert_valid(self) -> None:
        if self.failure:
            raise CodeModeReplayError(self.failure)
        record = self._record(self.durable.task(self.task_id))
        children = {
            effect.effect_id
            for effect in self.durable.effects(self.task_id)
            if effect.intent.get("parent_effect_id") == self.parent_effect_id
        }
        if self._children_seen != children or self._state_index != len(record["state"]):
            raise CodeModeReplayError("CodeMode recovery did not consume its recorded control flow")
