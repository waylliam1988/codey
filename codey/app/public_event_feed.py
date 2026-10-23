"""Shared immutable public encoding and independently bounded subscribers."""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from codey.app.event_payloads import browser_event_payload

PUBLIC_FIELDS = {
    "conversation_inventory_changed": frozenset({"type"}),
    "durable_commit": frozenset({"type", "session_id", "commit_seq", "kind", "committed_at", "schema_version", "entry_ids", "task_id", "revision", "head_entry_id", "run_id", "submission_id", "input_id", "deleted", "branch_entry_id"}),
    "resync_required": frozenset({"type", "reason", "cursor", "dropped"}),
    "reply": frozenset({"type", "session_id", "run_id", "text", "turn"}),
    "reply_stream": frozenset({"type", "session_id", "run_id", "text", "delta", "turn", "done", "generation", "stream_id", "sequence", "state"}),
    "conversation_input": frozenset({"type", "session_id", "input"}),
    "user_question": frozenset({"type", "session_id", "run_id", "question", "id", "text", "questions"}),
    "user_question_state": frozenset({"type", "session_id", "question"}),
    "providers": frozenset({"type", "providers", "default", "probe_error"}),
    "shell_result": frozenset({"type", "session_id", "run_id", "id", "command", "command_preview", "command_sha256", "command_chars", "command_truncated", "cwd", "project", "status", "output", "exit_code", "execution_id", "approved", "error", "ok", "truncated", "failure"}),
    "shell_approval_status": frozenset({"type", "session_id", "run_id", "source_run_id", "id", "approval_id", "decision", "execution", "continuation", "execution_id", "continuation_run_id", "revision", "error", "code"}),
    "teach_request": frozenset({"type", "session_id", "run_id", "question", "text", "id", "options"}),
    "ghost_post_turn_warning": frozenset({"type", "session_id", "run_id", "text", "error"}),
}
PUBLIC_FIELDS["durable_commit"] |= {"edited_entry_id", "replaced_run_ids"}


class PrivateEvent(Exception):
    pass


@dataclass(frozen=True)
class EncodedEvent:
    sequence: int
    commit_seq: int
    session_id: str
    body: bytes


class FeedOverflow(Exception):
    pass


class Subscription:
    def __init__(self, *, max_events: int, max_bytes: int, sessions: frozenset[str] | None) -> None:
        self.max_events, self.max_bytes, self.sessions = max_events, max_bytes, sessions
        self.condition = threading.Condition()
        self.items: deque[EncodedEvent] = deque()
        self.bytes = 0
        self.high_water_bytes = 0
        self.closed = False
        self.cutoff = 0

    def offer(self, event: EncodedEvent) -> None:
        if self.sessions is not None and event.session_id not in self.sessions:
            return
        with self.condition:
            if self.closed:
                return
            if len(self.items) >= self.max_events or self.bytes + len(event.body) > self.max_bytes:
                self.closed = True
                self.items.clear()
                self.bytes = 0
            else:
                self.items.append(event)
                self.bytes += len(event.body)
                self.high_water_bytes = max(self.high_water_bytes, self.bytes)
            self.condition.notify_all()

    def get(self, timeout: float = 15) -> EncodedEvent:
        with self.condition:
            if not self.items and not self.closed:
                self.condition.wait(timeout)
            if self.closed:
                raise FeedOverflow("Subscriber must reconcile a new snapshot")
            if not self.items:
                raise queue.Empty
            item = self.items.popleft()
            self.bytes -= len(item.body)
            return item

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.items.clear()
            self.bytes = 0
            self.condition.notify_all()


class PublicEventFeed:
    def __init__(self, *, max_frame_bytes: int = 1024 * 1024, cache_bytes: int = 8 * 1024 * 1024) -> None:
        self.lock = threading.Lock()
        self.subscribers: set[Subscription] = set()
        self.sequence = 0
        self.encodes = 0
        self.max_frame_bytes = max_frame_bytes
        self.cache_limit = cache_bytes
        self.cache_size = 0
        self.cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()

    def _encode(self, key: tuple[str, int], event: dict[str, Any]) -> bytes:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        public = browser_event_payload(event)
        if public is None:
            fields = PUBLIC_FIELDS.get(str(event.get("type")))
            if fields is None:
                raise PrivateEvent
            public = {key: value for key, value in event.items() if key in fields}
        payload = public
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > self.max_frame_bytes:
            raise FeedOverflow("Public event exceeds frame budget")
        self.encodes += 1
        while self.cache and (self.cache_size + len(body) > self.cache_limit or len(self.cache) >= 1000):
            _, old = self.cache.popitem(last=False)
            self.cache_size -= len(old)
        if len(body) <= self.cache_limit:
            self.cache[key] = body
            self.cache_size += len(body)
        return body

    def encode(self, event: dict[str, Any], sequence: int = 0) -> EncodedEvent:
        commit = int(event.get("commit_seq") or 0)
        key = ("commit", commit) if event.get("type") == "durable_commit" else ("event", sequence)
        with self.lock:
            body = self._encode(key, event)
        return EncodedEvent(sequence, commit, str(event.get("session_id") or ""), body)

    def publish(self, sequence: int, event: dict[str, Any]) -> None:
        with self.lock:
            self.sequence = sequence
            if not self.subscribers:
                return
            commit = int(event.get("commit_seq") or 0)
            key = ("commit", commit) if event.get("type") == "durable_commit" else ("event", sequence)
            try:
                body = self._encode(key, event)
            except PrivateEvent:
                return
            except (ValueError, TypeError, FeedOverflow):
                for subscriber in self.subscribers:
                    subscriber.close()
                return
            encoded = EncodedEvent(sequence, commit, str(event.get("session_id") or ""), body)
            for subscriber in tuple(self.subscribers):
                subscriber.offer(encoded)

    def subscribe(self, *, max_events: int = 1000, max_bytes: int = 8 * 1024 * 1024,
                  sessions: frozenset[str] | None = None) -> Subscription:
        item = Subscription(max_events=max_events, max_bytes=max_bytes, sessions=sessions)
        with self.lock:
            item.cutoff = self.sequence
            self.subscribers.add(item)
        return item

    def unsubscribe(self, item: Subscription) -> None:
        with self.lock:
            self.subscribers.discard(item)
        item.close()

    def close(self) -> None:
        with self.lock:
            for item in self.subscribers:
                item.close()
            self.subscribers.clear()
            self.cache.clear()
            self.cache_size = 0


def stream(handler: Any, ctx: Any, feed: PublicEventFeed) -> None:
    """Subscribe before replay; private SSE cursors wrap shared public bytes."""
    from urllib.parse import parse_qs, urlparse

    from codey.app.http_plumbing import sse_cursor, write_sse_event

    query = parse_qs(urlparse(handler.path).query)
    event_cursor, header_commit = sse_cursor(handler.headers.get("Last-Event-ID") or (query.get("last_event_id") or [""])[0])
    try:
        commit_cursor = max(header_commit, int((query.get("after_commit_seq") or [0])[0]))
    except ValueError:
        commit_cursor = 0
    subscriber = feed.subscribe()
    previous_timeout = handler.connection.gettimeout()
    handler.connection.settimeout(10)
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        database_id = ctx.durable.database_id
        requested = bool(event_cursor or commit_cursor or query.get("database_id"))
        if (query.get("database_id") or [""])[0] != database_id or commit_cursor > ctx.durable.commit_sequence():
            commit_cursor = 0
        if not write_sse_event(handler, {"type": "hello", "database_id": database_id,
                                         "after_commit_seq": commit_cursor, "status": ctx.run_status()}):
            return
        if requested:
            from codey.app.durable_events import replay_commits

            for record in replay_commits(ctx.durable, commit_cursor):
                encoded = feed.encode(record.to_payload())
                commit_cursor = record.sequence
                _write(handler, encoded, subscriber.cutoff, commit_cursor)
            for seq, event in ctx.replay_events_after(event_cursor, max_event_id=subscriber.cutoff):
                if event.get("type") == "durable_commit" and int(event.get("commit_seq") or 0) <= commit_cursor:
                    continue
                try:
                    encoded = feed.encode(event, seq)
                except PrivateEvent:
                    continue
                _write(handler, encoded, seq, commit_cursor)
        while True:
            try:
                encoded = subscriber.get()
            except queue.Empty:
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                continue
            if encoded.commit_seq and encoded.commit_seq <= commit_cursor:
                continue
            commit_cursor = max(commit_cursor, encoded.commit_seq)
            _write(handler, encoded, encoded.sequence, commit_cursor)
    except (OSError, FeedOverflow):
        # Closing this connection is the loss signal. Its next connection
        # reconciles durable facts, never treats buffered bytes as delivered.
        return
    finally:
        feed.unsubscribe(subscriber)
        with contextlib.suppress(OSError):
            handler.connection.settimeout(previous_timeout)


def _write(handler: Any, event: EncodedEvent, sequence: int, commit: int) -> None:
    handler.wfile.write(f"id: {sequence}:{commit}\ndata: ".encode() + event.body + b"\n\n")
    handler.wfile.flush()
