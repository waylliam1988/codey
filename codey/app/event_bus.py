"""In-process event bus for UI and SSE delivery."""

from __future__ import annotations

import queue
import threading
from collections import deque


class SsePayload(dict):
    """Queue payload with an SSE cursor that stays out of the JSON body."""

    def __init__(self, payload: dict, *, event_id: int = 0) -> None:
        super().__init__(payload)
        self.event_id = event_id


class EventSubscriber(queue.Queue):
    """One SSE client queue plus an overflow marker."""

    def __init__(self, maxsize: int = 1000) -> None:
        super().__init__(maxsize=maxsize)
        self.dropped = 0
        self.replay_cutoff = 0


class EventBus:
    def __init__(self, *, replay_limit: int) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[EventSubscriber] = []
        self._sequence = 0
        self._replay: deque[tuple[int, dict]] = deque(maxlen=replay_limit)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def emit(self, event: dict) -> None:
        with self._lock:
            payload = dict(event)
            self._sequence += 1
            event_id = self._sequence
            self._replay.append((event_id, dict(payload)))
            for sub in list(self._subscribers):
                self._put_for_subscriber(
                    sub,
                    SsePayload(dict(payload), event_id=event_id),
                    payload,
                )

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
    ) -> list[tuple[int, dict]]:
        start = max(0, int(last_event_id or 0))
        with self._lock:
            cutoff = self._sequence if max_event_id is None else max(0, int(max_event_id))
            rows = [
                (event_id, dict(payload))
                for event_id, payload in self._replay
                if start < event_id <= cutoff
            ]
            if start > 0 and self._replay and start < self._replay[0][0]:
                rows.insert(0, (
                    0,
                    {
                        "type": "resync_required",
                        "reason": "sse_replay_window_expired",
                        "dropped": self._replay[0][0] - start,
                    },
                ))
            return rows

    @staticmethod
    def _put_for_subscriber(
        sub: EventSubscriber,
        queued_payload: SsePayload,
        payload: dict,
    ) -> None:
        try:
            sub.put_nowait(queued_payload)
            return
        except queue.Full:
            pass
        except Exception:
            return

        limit = max(0, int(sub.maxsize or 0))
        while limit >= 2 and sub.qsize() > limit - 2:
            try:
                sub.get_nowait()
            except queue.Empty:
                break
            sub.dropped += 1
        if limit >= 2 and payload.get("type") != "resync_required":
            try:
                sub.put_nowait(SsePayload({
                    "type": "resync_required",
                    "reason": "sse_queue_overflow",
                    "dropped": sub.dropped,
                }))
                sub.dropped = 0
            except Exception:
                pass
        try:
            sub.put_nowait(queued_payload)
        except Exception:
            pass
