"""CodeMode control-flow recovery metadata in the original durable checkpoint.

Child requests and results belong only to original effect intents and receipts.
State operations reference original checkpoint revisions instead of copying values.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import RLock
from typing import TypedDict, cast

from codey.codemode.state import DurableCodeModeState
from codey.codemode.types import ToolCatalogSnapshot
from codey.durable.model import TaskCheckpoint, thaw_json
from codey.durable.session import DurableSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object


def repair_catalog_digest(catalog: ToolCatalogSnapshot, tool_names: Sequence[str]) -> str:
    """Fingerprint only the frozen capabilities used by a repair.

    The full research catalog may gain safe helper tools after a source is
    opened (for example ``reopen_source``).  That state change does not alter
    the already committed child calls.  A repair still binds every capability
    it can replay: name, arguments, output schema, namespace, grant and
    execution flags.  Missing or changed entries therefore fail closed while
    unrelated catalog additions do not invalidate a return-only repair.
    """
    by_name = {entry.name: entry for entry in catalog.entries}
    payload: list[dict[str, object]] = []
    for name in tool_names:
        entry = by_name.get(name)
        if entry is None:
            raise CodeModeReplayError(f"repair tool is no longer authorized: {name}")
        payload.append({
            "name": entry.name,
            "description": entry.description,
            "parameters": canonical_json_object(entry.parameters),
            "output_schema": canonical_json_object(entry.output_schema),
            "namespace": entry.namespace,
            "grant": entry.grant,
            "read_only": entry.read_only,
            "parallel_safe": entry.parallel_safe,
        })
    return "sha256:" + digest(payload)


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


@dataclass(frozen=True)
class ReplayChildView:
    """Read-only identity derived from one already-settled child receipt."""

    position: int
    effect_id: str
    tool_name: str
    call_id: str
    args_digest: str
    result_ref: str


@dataclass
class CodeModeRepairContext:
    """One authorized, bounded receipt-only repair attempt."""

    token: str
    parent_effect_id: str
    failure_code: str
    catalog_digest: str
    children: tuple[ReplayChildView, ...]
    receipts: Mapping[str, ToolResult]
    _used_positions: set[int]

    def replay_child(self, position: int, tool_name: str, args: JsonObject) -> ToolResult:
        if position < 0 or position >= len(self.children):
            raise CodeModeReplayError("repair issued a child call outside the original sequence")
        expected = self.children[position]
        if tool_name != expected.tool_name:
            raise CodeModeReplayError("repair changed the child tool")
        from codey.runtime.effects.effect_records import compute_args_digest

        if compute_args_digest(args) != expected.args_digest:
            raise CodeModeReplayError("repair changed child arguments")
        result = self.receipts.get(expected.effect_id)
        if result is None or result.call.call_id != expected.call_id:
            raise CodeModeReplayError("repair child receipt identity is unavailable")
        self._used_positions.add(position)
        return replace(result, audit={
            **result.audit,
            "result_ref": expected.result_ref,
            "recovery_disposition": "receipt_replay",
        })

    def assert_complete(self) -> None:
        if self._used_positions != set(range(len(self.children))):
            raise CodeModeReplayError("repair did not consume the original child sequence")


def repair_token(
    session_id: str,
    parent_effect_id: str,
    failure_code: str,
    child_effect_ids: Sequence[str],
    catalog_digest: str,
) -> str:
    return "cmr_" + digest({
        "session_id": session_id,
        "parent_effect_id": parent_effect_id,
        "failure_code": failure_code,
        "child_effect_ids": list(child_effect_ids),
        "catalog_digest": catalog_digest,
    })[:16]


def repair_metadata(
    session_id: str,
    parent_effect_id: str,
    failure_code: str,
    child_effect_ids: Sequence[str],
    catalog_digest: str,
) -> dict[str, object]:
    return {
        "token": repair_token(session_id, parent_effect_id, failure_code, child_effect_ids, catalog_digest),
        "session_id": session_id,
        "parent_effect_id": parent_effect_id,
        "failure_code": failure_code,
        "catalog_digest": catalog_digest,
    }


def resolve_repair_context(
    session: object,
    token: str,
    *,
    session_id: str,
    run_id: str,
    catalog: ToolCatalogSnapshot,
) -> CodeModeRepairContext:
    """Resolve a token through the existing in-memory receipt projection."""
    raw_results = getattr(session, "_memory_results", None)
    if not isinstance(raw_results, Mapping):
        raise CodeModeReplayError("repair receipt projection is unavailable")
    receipts = {
        key: value for key, value in raw_results.items()
        if isinstance(key, str) and isinstance(value, ToolResult)
    }
    metadata: Mapping[str, object] | None = None
    parent_effect_id = ""
    parent_result: ToolResult | None = None
    for result in receipts.values():
        repair = result.canonical.get("repair")
        if isinstance(repair, Mapping) and repair.get("token") == token:
            metadata = repair
            parent_effect_id = str(repair.get("parent_effect_id", ""))
            parent_result = result
            break
    if metadata is None or parent_result is None or not parent_effect_id:
        raise CodeModeReplayError("repair token is unknown or expired")
    if metadata.get("session_id") != session_id:
        raise CodeModeReplayError("repair token belongs to a different session")
    if not parent_effect_id.startswith(f"{run_id}:"):
        raise CodeModeReplayError("repair token belongs to a different run")
    failure_code = str(metadata.get("failure_code", ""))
    if failure_code not in {"missing_return", "result_encoding"}:
        raise CodeModeReplayError("repair is not authorized for this failure")
    for result in receipts.values():
        if result.canonical.get("repaired_from") == parent_effect_id:
            raise CodeModeReplayError("repair token has already been used")
    raw_children = parent_result.canonical.get("child_effect_ids")
    if not isinstance(raw_children, list) or not raw_children or not all(isinstance(item, str) for item in raw_children):
        raise CodeModeReplayError("repair child receipts are unavailable")
    children: list[ReplayChildView] = []
    from codey.runtime.effects.effect_records import compute_args_digest

    for position, effect_id in enumerate(raw_children):
        child = receipts.get(effect_id)
        if child is None:
            raise CodeModeReplayError("repair child receipt is missing or unsettled")
        result_ref = child.audit.get("result_ref")
        if not isinstance(result_ref, str) or not result_ref:
            result_ref = effect_id
        children.append(ReplayChildView(
            position=position, effect_id=effect_id, tool_name=child.call.name,
            call_id=child.call.call_id, args_digest=compute_args_digest(child.call.args),
            result_ref=result_ref,
        ))
    current_catalog_digest = repair_catalog_digest(catalog, [child.tool_name for child in children])
    if metadata.get("catalog_digest") != current_catalog_digest:
        raise CodeModeReplayError("repair capability changed; refusing receipt replay")
    return CodeModeRepairContext(
        token=token, parent_effect_id=parent_effect_id, failure_code=failure_code,
        catalog_digest=current_catalog_digest, children=tuple(children), receipts=receipts,
        _used_positions=set(),
    )


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
        self._failure = ""

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

    def invalidate(self, reason: str) -> None:
        """Permanently close this invocation, retaining its first fatal cause."""
        with self._lock:
            if not self._failure:
                self._failure = reason

    @contextmanager
    def admission(self) -> Generator[None]:
        """Serialize invalidation with intent admission and state transactions.

        The caller releases this guard before executing an admitted tool. Its
        eventual settlement remains allowed even if a sibling invalidates us.
        """
        with self._lock:
            if self._failure:
                raise CodeModeReplayError(self._failure)
            try:
                yield
            except Exception as exc:
                self.invalidate(str(exc))
                raise

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
        with self.admission():
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

    def store(self, key: str, value: object) -> None:
        self._state_step("store", key, value)

    def load(self, key: str) -> object:
        return self._state_step("load", key)

    def _state_step(self, op: str, key: str, value: object = None) -> object:
        with self.admission():
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

    def assert_valid(self) -> None:
        if self._failure:
            raise CodeModeReplayError(self._failure)
        record = self._record(self.durable.task(self.task_id))
        children = {
            effect.effect_id
            for effect in self.durable.effects(self.task_id)
            if effect.intent.get("parent_effect_id") == self.parent_effect_id
        }
        if self._children_seen != children or self._state_index != len(record["state"]):
            raise CodeModeReplayError("CodeMode recovery did not consume its recorded control flow")
