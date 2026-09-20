"""One shared switch against recursive model assistance.

Teach, doctor, review, and flow-recovery sends can each trigger further
model assistance (a recovery handler runs a provider send, which can hit a
teach pause, ...). Exactly one thread-local depth counter gates all of them:
entering any ``suppress_assistance`` region -- controls-side or flow-side --
pauses every other assistance entry point until it unwinds.

Previously each side owned its own counter, so a controls-suppressed region
still allowed flow recovery (and vice versa): recursion protection with a
hole in the middle. Callers keep using
``provider_controls.suppress_assistance`` / ``provider_flow.suppress_...``
(they re-export this module); task boundaries clear the counter via
``reset_assistance`` from ``begin/end_task_context``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_context = threading.local()


@contextmanager
def suppress_assistance() -> Iterator[None]:
    depth = int(getattr(_context, "assistance_depth", 0))
    epoch = int(getattr(_context, "assistance_epoch", 0))
    _context.assistance_depth = depth + 1
    try:
        yield
    finally:
        # A task-boundary reset inside the region retires this guard: only
        # restore the stashed depth when no reset intervened, otherwise a
        # cleared switch would flicker back on while unwinding.
        if int(getattr(_context, "assistance_epoch", 0)) == epoch:
            _context.assistance_depth = depth


def assistance_suppressed() -> bool:
    return bool(getattr(_context, "assistance_depth", 0))


def reset_assistance() -> None:
    if hasattr(_context, "assistance_depth"):
        delattr(_context, "assistance_depth")
    _context.assistance_epoch = int(getattr(_context, "assistance_epoch", 0)) + 1


__all__ = [
    "assistance_suppressed",
    "reset_assistance",
    "suppress_assistance",
]
