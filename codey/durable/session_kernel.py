"""Single durable synchronization, fencing, and commit owner.

The public session inherits this kernel so legacy entry points and operation
facades share exactly one store, reentrant lock, failure state, attempt binding,
and commit/close subscriptions. Domain algorithms add no independent locks.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from typing import Concatenate, ParamSpec, TypeVar

from codey.durable.approval_transition import materialize_approval
from codey.durable.errors import CommitConflict, IdempotencyConflict, SessionFailedError, SessionSealedError
from codey.durable.history import HistoryReader
from codey.durable.model import CommitRecord, CommitResult, DurableMutation, EffectRecord, TaskCheckpoint, TaskStatus
from codey.durable.store import DurableStore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_Kernel = TypeVar("_Kernel", bound="SessionKernel")
_P = ParamSpec("_P")


def _mutation_line(
    method: Callable[Concatenate[_Kernel, _P], _T],
) -> Callable[Concatenate[_Kernel, _P], _T]:
    """Keep the complete read/derive/commit operation on the one session line."""
    @wraps(method)
    def run(self: _Kernel, /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        with self._write_lock:
            self._ensure_usable()
            return method(self, *args, **kwargs)

    return run


class SessionKernel:
    """Own the synchronization and transaction semantics of one session."""

    @property
    def database_id(self) -> str:
        return self.store.database_id

    @contextmanager
    def control_line(self) -> Generator[None, None, None]:
        """Coordinate control reads/admission with the existing mutation line.

        Each write remains an atomic durable commit. Revocation precedes its
        cancellation cleanup so restart cannot dispatch the old generation.
        """
        with self._write_lock:
            self._ensure_usable()
            yield

    def __init__(
        self,
        store: DurableStore,
        *,
        on_commit: Callable[[CommitResult, DurableMutation], None] | None = None,
    ) -> None:
        self.store = store
        self._on_commit = on_commit
        self._write_lock = threading.RLock()
        self._sealed = False
        self._closed = False
        self._failure: BaseException | None = None
        self._internal_listeners: list[Callable[[CommitResult, DurableMutation], None]] = []
        self._commit_listeners: list[Callable[[CommitResult, DurableMutation], None]] = []
        self._close_listeners: list[Callable[[], None]] = []
        self._close_notified = False
        self._owner_id = f"owner-{uuid.uuid4().hex}"
        self._execution: ContextVar[tuple[str, str] | None] = ContextVar("durable_execution", default=None)

    def _query_line(self, operation: Callable[[], _T]) -> _T:
        """Run one query while sharing the session mutation line."""
        with self._write_lock:
            self._ensure_usable()
            return operation()

    @contextmanager
    def history_read(self, conversation_id: str) -> Generator[HistoryReader]:
        with self.store.history_snapshot() as reader:
            with self._write_lock:
                self._ensure_usable()
                # BEGIN alone is deferred: this SELECT fixes the WAL snapshot.
                reader.display_head(conversation_id)
            yield reader

    @_mutation_line
    def _commit(self, mutation: DurableMutation) -> CommitResult:
        self._ensure_writable()
        try:
            mutation = self._materialize_approval(mutation)
            if mutation.task is not None and mutation.expected_attempt_token is None:
                task_id = mutation.task.task_id
                try:
                    current = self.store.task(task_id)
                except KeyError:
                    current = None
                if current is not None:
                    bound = self._execution.get()
                    # A cancellation barrier may have terminalized the task
                    # and released its attempt token while the worker is
                    # committing its final runtime projection.  The old
                    # worker token must not make that already-authoritative
                    # terminal checkpoint look like a foreign writer.
                    if current.phase in {"cancelled", "failed", "completed"} and not current.attempt_token:
                        expected = ""
                    else:
                        expected = (bound[1] if bound is not None and bound[0] == task_id
                                    else current.attempt_token if current.state.get("attempt_owner") == self._owner_id else "")
                    mutation = replace(mutation, expected_attempt_token=expected)
            result = self.store.commit(mutation)
            self._publish_commit(result, mutation)
            return result
        except (CommitConflict, IdempotencyConflict, KeyError, TypeError, ValueError):
            raise
        except Exception as exc:
            self._fail(exc)
            raise

    def _materialize_approval(self, mutation: DurableMutation) -> DurableMutation:
        if mutation.approval is None:
            return mutation
        return materialize_approval(mutation, self.store.task(mutation.approval.task_id))

    def _ensure_writable(self) -> None:
        self._ensure_usable()

    def _ensure_usable(self) -> None:
        if self._failure is not None:
            raise SessionFailedError(self._failure) from self._failure
        if self._sealed:
            raise SessionSealedError(
                "durable session is sealed after a storage failure; reopen it"
            )
        if self._closed:
            raise SessionSealedError("durable session is closed; reopen it")

    def _fail(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
            self._sealed = True
            self._notify_close()

    @_mutation_line
    def transact_task(
        self,
        task_id: str,
        derive: Callable[[TaskCheckpoint, tuple[EffectRecord, ...], int], DurableMutation | None],
    ) -> CommitResult | None:
        """Derive and commit one typed mutation on the session mutation line.

        The reducer is pure: external work starts only after this returns.
        All checkpoint/effect reads, validation and publication share the
        same line; the store still checks revisions against other writers.
        """
        self._ensure_writable()
        current = self.store.task(task_id)
        _head, revision = self.store.head(current.conversation_id)
        mutation = derive(current, tuple(self.store.effects_for_task(task_id)), revision)
        if mutation is None:
            return None
        if (mutation.task is None or mutation.task.task_id != task_id
                or mutation.task.revision != current.revision + 1
                or mutation.expected_revision != revision):
            raise ValueError("task transaction must advance its captured checkpoint")
        return self._commit(mutation)

    @contextmanager
    def execution(self, task_id: str, attempt_token: str) -> Generator[None]:
        """Bind a queued worker to the exact attempt captured at dispatch."""
        with self._write_lock:
            self._ensure_writable()
            current = self.store.task(task_id)
            if current.status != TaskStatus.RUNNING or current.attempt_token != attempt_token:
                raise CommitConflict("task execution attempt changed")
        binding = self._execution.set((task_id, attempt_token))
        try:
            yield
        finally:
            self._execution.reset(binding)

    def _publish_commit(self, result: CommitResult, mutation: DurableMutation) -> None:
        if result.record is None:
            return
        listeners = list(self._internal_listeners)
        for listener in listeners:
            try:
                listener(result, mutation)
            except Exception as exc:
                self._fail(exc)
                raise
        external = list(self._commit_listeners)
        callback = self._on_commit
        if callback is not None:
            external.insert(0, callback)
        for listener in external:
            try:
                listener(result, mutation)
            except Exception:
                # External publication is a projection. A listener failure
                # cannot make an already committed durable mutation fail.
                logger.exception("durable commit publication failed")

    def observe_commits(
        self, listener: Callable[[CommitResult, DurableMutation], None]
    ) -> Callable[[], None]:
        with self._write_lock:
            self._ensure_usable()
            self._internal_listeners.append(listener)

        def unsubscribe() -> None:
            with self._write_lock:
                if listener in self._internal_listeners:
                    self._internal_listeners.remove(listener)

        return unsubscribe

    def subscribe_commits(
        self, listener: Callable[[CommitResult, DurableMutation], None]
    ) -> Callable[[], None]:
        with self._write_lock:
            self._ensure_usable()
            self._commit_listeners.append(listener)

        def unsubscribe() -> None:
            with self._write_lock:
                if listener in self._commit_listeners:
                    self._commit_listeners.remove(listener)

        return unsubscribe

    def subscribe_close(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._write_lock:
            self._ensure_usable()
            self._close_listeners.append(listener)

        def unsubscribe() -> None:
            with self._write_lock:
                if listener in self._close_listeners:
                    self._close_listeners.remove(listener)

        return unsubscribe

    def _notify_close(self) -> None:
        if self._close_notified:
            return
        self._close_notified = True
        listeners = list(self._close_listeners)
        self._close_listeners.clear()
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("durable session close listener failed")

    def close(self) -> None:
        with self.store.history_lifetime.exclusive(), self._write_lock:
            if self._closed:
                return
            self._closed = True
            self._notify_close()
            try:
                self.store.close()
            except Exception as exc:
                self._fail(exc)
                raise

    @_mutation_line
    def commit_sequence(self) -> int:
        return self.store.commit_sequence()

    @_mutation_line
    def commits_after(self, sequence: int, *, limit: int = 512) -> tuple[CommitRecord, ...]:
        return tuple(self.store.commits_after(sequence, limit=limit))
