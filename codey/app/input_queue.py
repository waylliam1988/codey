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
from typing import Any, cast

Submit = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class DurableConversationInputQueue:
    """Projection and dispatch facade backed only by ``DurableSession``.

    The browser still speaks the historical input-queue shape, but admission
    is a durable submission/task commit.  This is the only production input
    queue; queued state is derived from the durable task/submission records.
    """

    def __init__(self, durable: Any, *, emit: Callable[[dict[str, Any]], None]) -> None:
        self.durable = durable
        self.emit = emit
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.worker: threading.Thread | None = None

    @staticmethod
    def _state_for_task(task: Any) -> str:
        raw = task.state.get("input_state") if isinstance(task.state, dict) else None
        if raw in {"paused", "cancelled"}:
            return str(raw)
        status = str(task.status)
        if status == "completed" or task.phase == "completed":
            return "completed"
        if status == "cancelled" or task.phase == "cancelled":
            return "cancelled"
        if status == "failed" or task.phase == "failed":
            return "unconfirmed"
        if status == "running" or task.phase != "accepted":
            return "submitted"
        return "queued"

    @classmethod
    def _item_for_task(cls, task: Any) -> dict[str, Any] | None:
        state = task.state
        if not isinstance(state, Mapping):
            return None
        identity = state.get("input_identity")
        request = state.get("input_request")
        if not isinstance(identity, str) or not identity or not isinstance(request, Mapping):
            return None
        item_state = cls._state_for_task(task)
        normalized = _jsonable(dict(request))
        if not isinstance(normalized, dict):
            return None
        request = cast(dict[str, Any], normalized)
        return {
            "schema_version": 1,
            "id": identity,
            "request": copy.deepcopy(request),
            "state": item_state,
            "revision": int(task.revision),
            "origin": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        }

    def _items(self, session_id: str = "") -> builtins.list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for task in self.durable.tasks():
            item = self._item_for_task(task)
            if item is None:
                continue
            if session_id and item["request"].get("session_id") != session_id:
                continue
            items.append(item)
        return items

    def get(self, identity: str) -> dict[str, Any] | None:
        with self.lock:
            for item in self._items():
                if item["id"] == identity:
                    return item
            return None

    def list(self, session_id: str = "") -> builtins.list[dict[str, Any]]:
        with self.lock:
            return self._items(session_id)

    def create(self, identity: str, request: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
            raise ValueError("invalid input identity")
        if not request.get("session_id") or not request.get("run_id"):
            raise ValueError("invalid input identity")
        if not isinstance(request.get("task"), str) or not 0 < len(request["task"].strip()) <= 64_000:
            raise ValueError("invalid input text")
        # Admission freezes the same provider contract as the direct /api/run
        # path.  The browser sends a compact model selection; a resumed task
        # must carry the complete, validated selection instead of trying to
        # reconstruct it from current settings.
        from codey.providers.snapshot import capture_provider_selection, provider_snapshot

        provider_id = str(request.get("provider") or "")
        raw_selection = request.get("model_selection")
        selection = (
            capture_provider_selection(provider_id, raw_selection)
            if raw_selection is not None
            else None
        )
        admitted_request = copy.deepcopy(request)
        if selection is not None:
            admitted_request["model_selection"] = selection.to_payload()
        with self.lock:
            existing = self.get(identity)
            if existing is not None:
                if existing["origin"] != hashlib.sha256(json.dumps(admitted_request, sort_keys=True).encode()).hexdigest():
                    raise ValueError("input identity conflict")
                return existing
            snapshot = provider_snapshot(provider_id, selection)
            self.durable.admit_input(
                conversation_id=str(request["session_id"]),
                request_id=identity,
                task_id=str(request["run_id"]),
                text=str(request["task"]),
                provider_snapshot=snapshot,
                task_state={
                    "input_identity": identity,
                    "input_request": admitted_request,
                    "input_state": "queued",
                    **admitted_request,
                },
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
            item = self.get(str(item["id"])) or item
        try:
            status, result = submit(copy.deepcopy(item["request"]))
        except Exception as exc:
            status, result = 500, {"error": str(exc)[:500]}
        if status < 200 or status >= 300 or result.get("ok") is not True:
            with self.lock, contextlib.suppress(Exception):
                self.durable.unclaim_task(task_id)
        self.emit({"type": "conversation_input", "session_id": item["request"]["session_id"], "input": self.get(str(item["id"])) or item})
        return True

    def bind(self, ready: Callable[[], bool], submit: Submit) -> None:
        # DurableTaskScheduler owns admission dispatch. Keeping a second
        # worker here would create a duplicate execution race.
        return None

    def edit(self, identity: str, revision: int, text: str) -> dict[str, Any]:
        raise ValueError("durable input entries are immutable; submit a new request")

    def remove(self, identity: str, revision: int) -> dict[str, Any]:
        with self.lock:
            item = self.get(identity)
            if item is None:
                raise KeyError(f"unknown input identity: {identity}")
            if int(item["revision"]) != int(revision):
                raise ValueError("input revision conflict")
            if item["state"] != "queued":
                raise ValueError("input is already placed or settled")
            request = item["request"]
            if not isinstance(request, Mapping) or not isinstance(request.get("run_id"), str):
                raise ValueError("durable input request is missing run_id")
            self.durable.request_cancel(str(request["run_id"]), reason="aborted", expected_revision=int(revision))
            updated = self.get(identity)
        if updated is None:
            raise RuntimeError("durable input cancellation did not produce a projection")
        self.emit({"type": "conversation_input", "session_id": request.get("session_id", ""), "input": updated})
        self.wake.set()
        return updated

    def resume(self, identity: str, revision: int) -> dict[str, Any]:
        raise ValueError("durable input resumption is handled by task scheduler")

    def pause(self, session_id: str = "") -> None:
        self.wake.set()

    def completed(self, run_id: str, terminal: dict[str, Any]) -> None:
        self.wake.set()

    def close(self) -> bool:
        self.closed.set()
        self.wake.set()
        return True
