"""In-process event bus for UI and SSE delivery."""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from codey.utils.refs import parse_int

logger = logging.getLogger(__name__)


RUN_EVENT_TYPES = {
    "task_start",
    "turn",
    "tool",
    "tool_started",
    "info",
    "reply",
    "review",
    "shell_request",
    "shell_result",
    "ghost_post_turn_warning",
    "teach_request",
    "task_done",
    "status",
}


class RunScope(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...


def stamp_run_scope(payload: dict[str, object], active: RunScope | None) -> dict[str, object]:
    """Fill the active run/session onto a run-scoped event payload.

    Pure helper (no bus state): run-scoped types without an explicit run_id
    inherit the active run when the session matches or is unset.
    """
    if (
        active is not None
        and payload.get("type") in RUN_EVENT_TYPES
        and payload.get("run_id") in (None, "", active.run_id)
        and payload.get("session_id") in (None, "", active.session_id)
    ):
        if not payload.get("run_id"):
            payload["run_id"] = active.run_id
        if not payload.get("session_id"):
            payload["session_id"] = active.session_id
    return payload


class SsePayload(dict[str, object]):
    """Queue payload with an SSE cursor that stays out of the JSON body."""

    def __init__(self, payload: Mapping[str, object], *, event_id: int = 0) -> None:
        super().__init__(payload)
        self.event_id = event_id


class EventSubscriber(queue.Queue[SsePayload]):
    """One SSE client queue plus an overflow marker."""

    def __init__(self, maxsize: int = 1000) -> None:
        super().__init__(maxsize=maxsize)
        self.dropped = 0
        self.replay_cutoff = 0


_Observer = Callable[[int, dict[str, object]], None]


@dataclass(frozen=True)
class _Publication:
    sequence: int
    payload: dict[str, object]
    observers: tuple[_Observer, ...]
    subscribers: tuple[EventSubscriber, ...]


class EventBus:
    def __init__(self, *, replay_limit: int) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[EventSubscriber] = []
        self._sequence = 0
        self._replay: deque[tuple[int, dict[str, object]]] = deque(maxlen=replay_limit)
        self._observers: list[_Observer] = []
        self._pending: deque[_Publication] = deque()
        self._notifying = False

    def observe(self, callback: _Observer) -> Callable[[], None]:
        with self._lock:
            self._observers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._observers:
                    self._observers.remove(callback)
        return unsubscribe

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def emit(self, event: Mapping[str, object]) -> None:
        with self._lock:
            payload = dict(event)
            self._sequence += 1
            event_id = self._sequence
            self._replay.append((event_id, dict(payload)))
            self._pending.append(_Publication(event_id, payload, tuple(self._observers), tuple(self._subscribers)))
            if self._notifying:
                return
            # A reentrant emit queues its notifications behind this event.
            # Other threads still wait until ordered synchronous delivery ends.
            self._notifying = True
            try:
                while self._pending:
                    publication = self._pending.popleft()
                    for observer in publication.observers:
                        try:
                            observer(publication.sequence, publication.payload)
                        except Exception:
                            logger.exception("event observer failed")
                    for sub in publication.subscribers:
                        self._put_for_subscriber(
                            sub,
                            SsePayload(publication.payload, event_id=publication.sequence),
                            publication.payload,
                        )
            finally:
                self._notifying = False

    def subscribe(self, *, maxsize: int = 1000) -> EventSubscriber:
        sub = EventSubscriber(maxsize=maxsize)
        with self._lock:
            sub.replay_cutoff = self._sequence
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: EventSubscriber) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def replay_events_after(
        self,
        last_event_id: int,
        *,
        max_event_id: int | None = None,
    ) -> list[tuple[int, dict[str, object]]]:
        if isinstance(last_event_id, bool):
            start = 0
        else:
            parsed_start = parse_int(last_event_id or 0)
            start = max(0, parsed_start if parsed_start is not None else 0)
        with self._lock:
            if max_event_id is None:
                cutoff = self._sequence
            else:
                parsed_cutoff = parse_int(max_event_id)
                cutoff = min(
                    self._sequence,
                    max(0, parsed_cutoff) if parsed_cutoff is not None else self._sequence,
                )
            rows = [
                (event_id, dict(payload))
                for event_id, payload in self._replay
                if start < event_id <= cutoff
            ]
            if start > cutoff:
                return [(cutoff, {"type": "resync_required", "reason": "sse_cursor_ahead", "cursor": cutoff})]
            if start > 0 and self._replay and start < self._replay[0][0] - 1:
                # Expired window: force a full reconcile. Return only the
                # marker (no retained rows) stamped past the caller's cursor,
                # so adopting it strictly advances Last-Event-ID and the same
                # marker is never replayed twice.
                return [(
                    cutoff,
                    {
                        "type": "resync_required",
                        "reason": "sse_replay_window_expired",
                        "dropped": self._replay[0][0] - start - 1,
                        "cursor": cutoff,
                    },
                )]
            return rows

    @staticmethod
    def _put_for_subscriber(
        sub: EventSubscriber,
        queued_payload: SsePayload,
        payload: Mapping[str, object],
    ) -> None:
        try:
            sub.put_nowait(queued_payload)
            return
        except queue.Full:
            pass
        except Exception:
            # queue.Full is the expected overflow path (handled below);
            # any other queue failure must stay observable, never silent.
            logger.exception("event bus subscriber put failed")
            return

        try:
            limit = max(0, (sub.maxsize or 0))
        except (TypeError, ValueError, OverflowError):
            limit = 0
        while limit >= 2 and sub.qsize() > limit - 2:
            try:
                sub.get_nowait()
            except queue.Empty:
                break
            sub.dropped += 1
        pending_dropped = sub.dropped
        marker_enqueued = payload.get("type") == "resync_required"
        if limit >= 2 and not marker_enqueued:
            try:
                sub.put_nowait(SsePayload({
                    "type": "resync_required",
                    "reason": "sse_queue_overflow",
                    "dropped": pending_dropped,
                }))
                marker_enqueued = True
            except queue.Full:
                marker_enqueued = False
            except Exception:
                logger.exception("event bus overflow marker put failed")
                marker_enqueued = False
        try:
            sub.put_nowait(queued_payload)
        except queue.Full:
            # Keep the loss observable: the next emit retries the marker
            # with the retained count instead of silently resetting it.
            sub.dropped = pending_dropped + 1
            return
        except Exception:
            logger.exception("event bus subscriber retry put failed")
            return
        if marker_enqueued or limit < 2 or payload.get("type") == "resync_required":
            sub.dropped = 0
