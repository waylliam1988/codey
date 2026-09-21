"""shrink.py: reduce a failing soak script to a minimal repro (QuickCheck-style).

A soak failure at step 918,273 is not debuggable by hand. Given the
recorded script and the original ``SoakFailure``, this module returns a
much shorter script that still reproduces *the same* violation:

1. ``shrink_prefix`` bisects the minimal failing prefix (O(log n) replays).
2. ``ddmin`` then removes chunks and single steps inside that prefix
   while the violation reproduces.

"Same violation" is deliberately narrow: an ``InvariantViolation`` must
carry the same ``[tag]``; any other failure must match type and message.
Replay self-checks (``"replay diverged..."``) never count: they fire when
a reduction renumbers effect ids, which is a shrink artifact, not the bug.
Shrink is best-effort and budget-capped; the full recorded script in the
failure artifact is always the ground truth.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from tests.stress.oracle import InvariantViolation
from tests.stress.scheduler import SoakFailure, replay_script

Predicate = Callable[[list[dict]], bool]


def _cause_key(failure: SoakFailure) -> tuple[str, str]:
    cause = failure.cause
    if isinstance(cause, InvariantViolation):
        text = str(cause)
        tag = text.split("]", 1)[0] + "]" if text.startswith("[") else text
        return ("invariant", tag)
    return ("error", f"{type(cause).__name__}:{cause}")


def replay_fails(script: list[dict], key: tuple[str, str]) -> bool:
    """Replay a candidate script; True iff it reproduces the keyed violation."""
    if not script:
        return False
    tmp = tempfile.TemporaryDirectory()
    try:
        try:
            replay_script(script, Path(tmp.name) / "state")
        except SoakFailure as failure:
            if str(failure.cause).startswith("replay diverged"):
                return False
            return _cause_key(failure) == key
        except InvariantViolation as violation:
            return ("invariant", str(violation).split("]", 1)[0] + "]") == key
        except Exception:
            return False
        return False
    finally:
        tmp.cleanup()


def shrink_prefix(
    script: list[dict],
    predicate: Predicate,
    *,
    max_evals: int = 64,
) -> list[dict]:
    """Bisect the shortest failing prefix. ``script`` itself must fail."""
    if not predicate(script):
        raise ValueError("shrink_prefix needs a failing script")
    evals = 1
    low, high = 1, len(script)
    while low < high and evals < max_evals:
        mid = (low + high) // 2
        evals += 1
        if predicate(script[:mid]):
            high = mid
        else:
            low = mid + 1
    return script[:high]


def ddmin(script: list[dict], predicate: Predicate, *, max_evals: int = 256) -> list[dict]:
    """Delta-debugging minimization: remove chunks, then single steps."""
    if len(script) <= 1:
        return script
    evals = 0
    current = list(script)
    granularity = 2
    while len(current) > 1 and evals < max_evals:
        chunk = max(1, len(current) // granularity)
        reduced = False
        for start in range(0, len(current), chunk):
            if evals >= max_evals:
                break
            candidate = current[:start] + current[start + chunk:]
            evals += 1
            if candidate and predicate(candidate):
                current = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if not reduced:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)
    return current


def shrink_script(
    script: list[dict],
    failure: SoakFailure | None = None,
    *,
    budget_s: float = 120.0,
    check_every: int = 0,
) -> list[dict]:
    """Reduce ``script`` to a minimal repro of ``failure`` within budget."""
    deadline = time.perf_counter() + max(1.0, budget_s)
    key = _cause_key(failure) if failure is not None else None

    def predicate(candidate: list[dict]) -> bool:
        if time.perf_counter() >= deadline:
            raise TimeoutError("shrink budget exhausted")
        if key is None:
            return replay_fails(candidate, ("error", ""))
        return replay_fails(candidate, key)

    if not script or not predicate(script):
        raise ValueError("shrink_script needs a failing script")
    prefix = shrink_prefix(script, predicate)
    if time.perf_counter() >= deadline:
        return prefix
    try:
        return ddmin(prefix, predicate)
    except TimeoutError:
        return prefix


__all__ = [
    "Predicate",
    "ddmin",
    "replay_fails",
    "shrink_prefix",
    "shrink_script",
]
