"""Canonical durable facts: the only thing stress oracles may assert.

A "restart" drops every in-memory object and reopens the same state
directory, so only bytes on disk can converge. ``canonical_facts`` reads
every durable surface through the production read paths (which include
tail-repair) and returns JSON-stable structures. ``normalize`` additionally
rewrites volatile identifiers (mutation batch uuids, effect nonce suffixes)
to first-appearance ordinals so two runs of the same seed compare equal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExpectedOutcome:
    operation_id: str
    terminal_state: str
    committed_effects: tuple[str, ...] = ()
    rejected_effects: tuple[str, ...] = ()
    completion_proof: str | None = None


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}"
)
_HEX_TAIL_RE = re.compile(r"_[0-9a-f]{8,}$")


def _stable(value: object, aliases: dict[str, str]) -> object:
    def _ordinal(token: str) -> str:
        if token not in aliases:
            aliases[token] = f"<id{len(aliases)}>"
        return aliases[token]

    if isinstance(value, str):
        value = _UUID_RE.sub(lambda m: _ordinal(m.group(0)), value)
        value = _HEX_TAIL_RE.sub(lambda m: "_" + _ordinal(m.group(0)[1:]), value)
        return value
    if isinstance(value, dict):
        return {key: _stable(item, aliases) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_stable(item, aliases) for item in value]
    return value


def normalize(facts: dict) -> dict:
    """Rewrite volatile identifiers to first-appearance ordinals."""
    return _stable(facts, {})


def canonical_json(facts: dict) -> str:
    return json.dumps(normalize(facts), ensure_ascii=False, sort_keys=True)


@dataclass
class DurableSnapshot:
    """One canonical reading of every durable surface of a stress world."""

    log_rows: tuple = ()
    ghost_rows: tuple = ()
    delivery_batches: tuple = ()
    approvals: tuple = ()
    journal_events: tuple = ()

    def to_facts(self) -> dict:
        return {
            "log_rows": list(self.log_rows),
            "ghost_rows": list(self.ghost_rows),
            "delivery_batches": list(self.delivery_batches),
            "approvals": list(self.approvals),
            "journal_events": list(self.journal_events),
        }


def fold_event_rows(rows) -> tuple:
    """Order-preserving dedupe of identical rows: replay must not double-apply."""
    seen: set[str] = set()
    out: list = []
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return tuple(out)


__all__ = [
    "DurableSnapshot",
    "ExpectedOutcome",
    "canonical_json",
    "fold_event_rows",
    "normalize",
]
