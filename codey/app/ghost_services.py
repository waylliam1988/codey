"""Lazy ownership of the optional Ghost stores.

Ghost is a background projection and memory subsystem.  Its stores are
constructed on first use so a normal task does not pay their file I/O cost,
and the construction itself stays outside ``AppContext.lock``.  Keeping this
small resource boundary separate leaves ``AppContext`` responsible for
application assembly rather than individual Ghost storage races.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.work_queue import GhostWorkQueueStore

_T = TypeVar("_T")
_MISSING = object()


def _close_discarded(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


class GhostStores:
    """Thread-safe lazy cache for all optional Ghost stores."""

    def __init__(self, state_home: str | Path | None, lock: threading.Lock) -> None:
        self._state_home = Path(state_home) if state_home is not None else None
        self._lock = lock
        self._values: dict[str, object | None] = {}

    def _get(self, name: str, factory: Callable[[Path], _T]) -> _T | None:
        with self._lock:
            existing = self._values.get(name, _MISSING)
            if existing is not _MISSING:
                return cast(_T | None, existing)
            state_home = self._state_home
        if state_home is None:
            return None

        # Store constructors perform filesystem work.  Never hold the
        # application lock while doing that work.
        fresh = factory(state_home)
        with self._lock:
            existing = self._values.get(name, _MISSING)
            if existing is _MISSING:
                self._values[name] = fresh
                return fresh
        _close_discarded(fresh)
        return cast(_T | None, existing)

    def _set(self, name: str, value: object | None) -> None:
        with self._lock:
            self._values[name] = value

    @property
    def inbox(self) -> GhostInboxStore | None:
        return self._get("inbox", GhostInboxStore)

    @inbox.setter
    def inbox(self, value: GhostInboxStore | None) -> None:
        self._set("inbox", value)

    @property
    def hebbian(self) -> GhostHebbianStore | None:
        return self._get("hebbian", GhostHebbianStore)

    @hebbian.setter
    def hebbian(self, value: GhostHebbianStore | None) -> None:
        self._set("hebbian", value)

    @property
    def continuity(self) -> GhostContinuityStore | None:
        return self._get("continuity", GhostContinuityStore)

    @continuity.setter
    def continuity(self, value: GhostContinuityStore | None) -> None:
        self._set("continuity", value)

    @property
    def sleep(self) -> GhostSleepStore | None:
        return self._get("sleep", GhostSleepStore)

    @sleep.setter
    def sleep(self, value: GhostSleepStore | None) -> None:
        self._set("sleep", value)

    @property
    def work_queue(self) -> GhostWorkQueueStore | None:
        return self._get("work_queue", GhostWorkQueueStore)

    @work_queue.setter
    def work_queue(self, value: GhostWorkQueueStore | None) -> None:
        self._set("work_queue", value)

    @property
    def affinity(self) -> GhostAffinityStore | None:
        return self._get("affinity", GhostAffinityStore)

    @affinity.setter
    def affinity(self, value: GhostAffinityStore | None) -> None:
        self._set("affinity", value)

    @property
    def observations(self) -> GhostObservationStore | None:
        return self._get("observations", GhostObservationStore)

    @observations.setter
    def observations(self, value: GhostObservationStore | None) -> None:
        self._set("observations", value)


class GhostStoreAccessors:
    """Typed facade kept on ``AppContext`` for existing callers."""

    _ghost_stores: GhostStores

    @property
    def ghost_inbox(self) -> GhostInboxStore | None:
        return self._ghost_stores.inbox

    @ghost_inbox.setter
    def ghost_inbox(self, value: GhostInboxStore | None) -> None:
        self._ghost_stores.inbox = value

    @property
    def ghost_hebbian(self) -> GhostHebbianStore | None:
        return self._ghost_stores.hebbian

    @ghost_hebbian.setter
    def ghost_hebbian(self, value: GhostHebbianStore | None) -> None:
        self._ghost_stores.hebbian = value

    @property
    def ghost_continuity(self) -> GhostContinuityStore | None:
        return self._ghost_stores.continuity

    @ghost_continuity.setter
    def ghost_continuity(self, value: GhostContinuityStore | None) -> None:
        self._ghost_stores.continuity = value

    @property
    def ghost_sleep(self) -> GhostSleepStore | None:
        return self._ghost_stores.sleep

    @ghost_sleep.setter
    def ghost_sleep(self, value: GhostSleepStore | None) -> None:
        self._ghost_stores.sleep = value

    @property
    def ghost_work_queue(self) -> GhostWorkQueueStore | None:
        return self._ghost_stores.work_queue

    @ghost_work_queue.setter
    def ghost_work_queue(self, value: GhostWorkQueueStore | None) -> None:
        self._ghost_stores.work_queue = value

    @property
    def ghost_affinity(self) -> GhostAffinityStore | None:
        return self._ghost_stores.affinity

    @ghost_affinity.setter
    def ghost_affinity(self, value: GhostAffinityStore | None) -> None:
        self._ghost_stores.affinity = value

    @property
    def ghost_observations(self) -> GhostObservationStore | None:
        return self._ghost_stores.observations

    @ghost_observations.setter
    def ghost_observations(self, value: GhostObservationStore | None) -> None:
        self._ghost_stores.observations = value


__all__ = ["GhostStoreAccessors", "GhostStores"]
