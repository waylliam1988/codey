"""Durable conversation inputs, with an admission fence before submission.

The queue lock never covers application callbacks. A dispatch whose outcome is
unknown is retained for status checking, never automatically submitted again.
"""
from __future__ import annotations

import builtins
import contextlib
import copy
import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, NotRequired, TypedDict

from codey.task.model import StartTask, task_input_payload
from codey.utils.object_fields import mapping_fields, object_fields, require_object_fields, sequence_items

if TYPE_CHECKING:
    from codey.durable.model import TaskCheckpoint
    from codey.durable.session import DurableSession
    from codey.runtime.core.api_selection import ApiRunSelection

Submit = Callable[[dict[str, object]], tuple[int, dict[str, object]]]


class InputItem(TypedDict):
    schema_version: int
    id: str
    request: dict[str, object]
    state: str
    revision: int
    origin: str
    consumed_by_run_id: object
    entry_id: object
    terminal: NotRequired[dict[str, object]]


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        fields: Mapping[object, object] = value
        return {str(key): _jsonable(item) for key, item in fields.items()}
    if sequence_items(value):
        return [_jsonable(item) for item in value]
    return value


def capture_input_selection(provider_id: str, value: object) -> ApiRunSelection | None:
    from codey.providers.catalog import API_CONNECTIONS
    from codey.providers.snapshot import capture_provider_selection
    from codey.runtime.core.api_selection import ApiRunSelection

    if provider_id not in API_CONNECTIONS:
        return None
    if value is not None and not isinstance(value, ApiRunSelection) and not mapping_fields(value):
        raise ValueError("invalid model selection")
    return capture_provider_selection(provider_id, value)


class DurableConversationInputQueue:
    """Projection and dispatch facade backed only by ``DurableSession``.

    The browser still speaks the historical input-queue shape, but admission
    is a durable submission/task commit.  This is the only production input
    queue; queued state is derived from the durable task/submission records.
    """

    def __init__(self, durable: DurableSession, *, emit: Callable[[dict[str, object]], None]) -> None:
        self.durable = durable
        self.emit = emit
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.worker: threading.Thread | None = None

    @staticmethod
    def _state_for_task(task: TaskCheckpoint) -> str:
        raw = task.state.get("input_state") if isinstance(task.state, Mapping) else None
        status = str(task.status)
        if raw == "consumed":
            return "consumed"
        if status == "completed" or task.phase == "completed":
            return "completed"
        if status == "cancelled" or task.phase == "cancelled":
            return "cancelled"
        if status == "failed" or task.phase == "failed":
            return "completed"
        if raw in {"paused", "cancelled"}:
            return str(raw)
        if raw == "queued":
            return "queued"
        if status == "running" or task.phase != "accepted":
            return "submitted"
        return "submitted" if raw == "placed" else "queued"

    def _item_for_task(self, task: TaskCheckpoint) -> InputItem | None:
        state = task.state
        identity = state.get("input_identity")
        raw_request = state.get("input_request")
        if not isinstance(identity, str) or not identity or not isinstance(raw_request, Mapping):
            return None
        item_state = self._state_for_task(task)
        normalized = _jsonable(raw_request)
        if not object_fields(normalized):
            return None
        request = normalized
        origin = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        # Admission identity remains immutable; queued revisions are projected
        # from the same task/submission that placement will consume.
        if isinstance(state.get("input_text"), str):
            request["task"] = state["input_text"]
        if state.get("input_mode") in {"steer", "follow_up"}:
            request["input_mode"] = state["input_mode"]
        item: InputItem = {
            "schema_version": 1,
            "id": identity,
            "request": copy.deepcopy(request),
            "state": item_state,
            "revision": task.revision,
            "origin": origin,
            "consumed_by_run_id": state.get("consumed_by_task_id", ""),
            "entry_id": state.get("input_entry_id", ""),
        }
        if str(task.status) == "failed":
            reason = self.durable.submission(task.submission_id).reason or "Task failed"
            item["terminal"] = {"type": "task_done", "session_id": task.conversation_id,
                                "run_id": task.task_id, "stop_reason": "error", "summary": reason}
        return item

    def _items(self, session_id: str = "") -> builtins.list[InputItem]:
        items: list[InputItem] = []
        for task in self.durable.tasks():
            item = self._item_for_task(task)
            if item is None:
                continue
            if session_id and item["request"].get("session_id") != session_id:
                continue
            items.append(item)
        return items

    def get(self, identity: str) -> InputItem | None:
        with self.lock:
            for item in self._items():
                if item["id"] == identity:
                    return item
            return None

    def list(self, session_id: str = "") -> builtins.list[InputItem]:
        with self.lock:
            return self._items(session_id)

    def create(self, identity: str, request: Mapping[str, object], *, model_selection: ApiRunSelection | None = None, message_metadata: dict[str, object] | None = None) -> InputItem:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
            raise ValueError("invalid input identity")
        if not request.get("session_id") or not request.get("run_id"):
            raise ValueError("invalid input identity")
        text = request.get("task")
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 64_000:
            raise ValueError("invalid input text")
        # Admission freezes the same provider contract as the direct /api/run
        # path.  The browser sends a compact model selection; a resumed task
        # must carry the complete, validated selection instead of trying to
        # reconstruct it from current settings.
        from codey.providers.snapshot import provider_snapshot

        provider_id = str(request.get("provider") or "")
        selection = model_selection or capture_input_selection(provider_id, request.get("model_selection"))
        admitted_request = copy.deepcopy(dict(request))
        if selection is not None:
            selection_payload: object = selection.to_payload()
            admitted_request["model_selection"] = require_object_fields(selection_payload)
        with self.lock:
            existing = self.get(identity)
            if existing is not None:
                if existing["origin"] != hashlib.sha256(json.dumps(admitted_request, sort_keys=True).encode()).hexdigest():
                    raise ValueError("input identity conflict")
                return existing
            snapshot = provider_snapshot(provider_id, selection)
            busy = any(
                task.conversation_id == str(request["session_id"])
                and str(task.status) not in {"completed", "failed", "cancelled"}
                and task.state.get("input_state") != "queued"
                for task in self.durable.tasks()
            )
            placement = "queued" if busy else "placed"
            self.durable.admit_input(
                conversation_id=str(request["session_id"]),
                request_id=identity,
                task_id=str(request["run_id"]),
                text=str(request["task"]),
                provider_snapshot=snapshot,
                task_state={
                    "input_identity": identity,
                    **({"message_metadata": message_metadata} if message_metadata else {}),
                    "input_request": admitted_request,
                    "input_state": "queued",
                    **admitted_request,
                    "input": task_input_payload(StartTask(text)),
                },
                placement=placement,
                mode=str(request.get("input_mode") or "follow_up"),
            )
            item = self.get(identity)
        if item is None:
            raise RuntimeError("durable input admission did not produce a projection")
        self.emit({"type": "conversation_input", "session_id": request["session_id"], "input": item})
        self.wake.set()
        return item

    def dispatch_one(self, ready: Callable[[], bool], submit: Submit) -> bool:
        if self.closed.is_set() or not ready():
            return False
        with self.lock:
            item = next((item for item in self._items() if item["state"] == "queued"), None)
            if item is None:
                return False
            task_id = str(item["request"]["run_id"])
            task = self.durable.task(task_id)
            if str(task.status) == "pending":
                self.durable.claim_task(task_id)
            elif str(task.status) != "running":
                return False
            item = self.get(item["id"]) or item
        try:
            status, result = submit(copy.deepcopy(item["request"]))
        except Exception as exc:
            status, result = 500, {"error": str(exc)[:500]}
        if status < 200 or status >= 300 or result.get("ok") is not True:
            with self.lock, contextlib.suppress(Exception):
                self.durable.unclaim_task(task_id)
        else:
            # Admission may leave the durable input queued while the task is
            # waiting for a conversation slot. Once the dispatch callback has
            # accepted it, publish the submitted state in the same canonical
            # task record so status and shutdown retention observe reality.
            with self.lock, contextlib.suppress(Exception):
                current = self.durable.task(task_id)
                if current.state.get("input_state") == "queued":
                    self.durable.update_task_state(
                        task_id,
                        {"input_state": "placed"},
                        expected_revision=current.revision,
                    )
        self.emit({"type": "conversation_input", "session_id": item["request"]["session_id"], "input": self.get(item["id"]) or item})
        return True

    def bind(self, ready: Callable[[], bool], submit: Submit) -> None:
        # DurableTaskScheduler owns admission dispatch. Keeping a second
        # worker here would create a duplicate execution race.
        return None

    def edit(self, identity: str, revision: int, text: str) -> InputItem:
        return self._revise(identity, revision, text=text)

    def steer(self, identity: str, revision: int) -> InputItem:
        return self._revise(identity, revision, mode="steer")

    def _revise(self, identity: str, revision: int, *, text: str | None = None,
                mode: str | None = None) -> InputItem:
        from codey.durable.errors import CommitConflict

        with self.lock:
            item = self.get(identity)
            if item is None:
                raise ValueError("input not found")
            try:
                self.durable.revise_queued_input(
                    str(item["request"]["run_id"]), expected_revision=revision, text=text, mode=mode,
                )
            except CommitConflict as exc:
                raise ValueError(str(exc)) from exc
            updated = self.get(identity)
        if updated is None:
            raise RuntimeError("input revision did not produce a projection")
        self.emit({"type": "conversation_input", "session_id": item["request"]["session_id"], "input": updated})
        self.wake.set()
        return updated

    def remove(self, identity: str, revision: int) -> InputItem:
        with self.lock:
            item = self.get(identity)
            if item is None:
                raise KeyError(f"unknown input identity: {identity}")
            if item["revision"] != revision:
                raise ValueError("input revision conflict")
            if item["state"] != "queued":
                raise ValueError("input is already placed or settled")
            request = item["request"]
            run_id = request.get("run_id")
            if not isinstance(run_id, str):
                raise ValueError("durable input request is missing run_id")
            self.durable.request_cancel(run_id, reason="aborted", expected_revision=revision)
            updated = self.get(identity)
        if updated is None:
            raise RuntimeError("durable input cancellation did not produce a projection")
        self.emit({"type": "conversation_input", "session_id": request.get("session_id", ""), "input": updated})
        self.wake.set()
        return updated

    def resume(self, identity: str, revision: int) -> InputItem:
        raise ValueError("durable input resumption is handled by task scheduler")

    def pause(self, session_id: str = "") -> None:
        self.wake.set()

    def completed(self, run_id: str, terminal: Mapping[str, object]) -> None:
        self.wake.set()

    def close(self) -> bool:
        self.closed.set()
        self.wake.set()
        return True
