"""Shared reasoning vocabulary and admission compilation rules."""
from __future__ import annotations

from collections.abc import Iterable

# ``off`` is an internal/UI sentinel. OpenCode's catalog uses declared effort
# values and does not advertise it as an effort level; the wire adapter maps
# it to the provider-specific ``none`` value when needed.
REASONING_EFFORTS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
CATALOG_REASONING_EFFORTS = REASONING_EFFORTS - {"off"}


def normalize_efforts(values: Iterable[object]) -> tuple[str, ...]:
    """Keep catalog effort order while removing unknown and duplicate values."""
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value in CATALOG_REASONING_EFFORTS and value not in result:
            result.append(value)
    return tuple(result)


def compile_selection(*, efforts: tuple[str, ...], toggle: bool,
                      effort: object = None, thinking: object = None) -> tuple[str | None, bool | None]:
    """Compile one Zen user choice into the two shared wire controls.

    Effort-capable models use the effort axis, including models that also
    advertise a toggle. Toggle-only models use the boolean axis. The two
    controls are never emitted together, which keeps provider behavior
    deterministic across OpenAI-compatible and Responses transports.
    """
    if effort is not None:
        if not isinstance(effort, str) or effort not in efforts:
            raise ValueError("selected model does not advertise this reasoning effort")
        if thinking is not None:
            raise ValueError("reasoning effort and thinking toggle cannot be selected together")
        return effort, None
    if thinking is not None:
        if type(thinking) is not bool:
            raise ValueError("thinking must be true, false, or null")
        if not toggle or efforts:
            raise ValueError("selected model does not advertise a standalone thinking toggle")
        return None, thinking
    return None, None
