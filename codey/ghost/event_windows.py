"""Replay checkpoints retain original events in their existing domain tables."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Literal

EventStream = Literal["inbox", "continuity", "work_queue", "sleep", "hebbian", "affinity", "observations"]


def _event_position(database: sqlite3.Connection, stream: EventStream, *, position: int | None = None) -> int:
    parameters: tuple[str, ...]
    if stream in {"hebbian", "affinity"}:
        table, selector, parameters = "graph_events", "graph=?", (stream,)
    elif stream == "observations":
        table, selector, parameters = "observation_history", "1=1", ()
    else:
        table, selector, parameters = "memory_events", "collection=?", (stream,)
    if position is None:
        query = f"SELECT coalesce(max(sequence),0) FROM {table} WHERE {selector}"
    else:
        query = f"SELECT count(*) FROM {table} WHERE {selector} AND sequence=?"
    values: tuple[str | int, ...] = (*parameters, position) if position is not None else parameters
    row: sqlite3.Row | None = database.execute(query, values).fetchone()
    value: object = None
    if row is not None:
        value = row[0]
    if type(value) is not int or value < 0:
        raise ValueError("invalid Ghost event position")
    return value


def event_window(database: sqlite3.Connection, stream: EventStream) -> int:
    row: sqlite3.Row | None = database.execute(
        "SELECT after_sequence,applied_after_sequence FROM ghost_replay_windows WHERE stream=?", (stream,),
    ).fetchone()
    position: object = None
    applied: object = None
    if row is not None:
        position = row[0]
        applied = row[1]
    if type(position) is not int or position < 0 or type(applied) is not int or applied < 0:
        raise ValueError("missing or invalid Ghost replay window")
    if position != applied:
        raise ValueError("Ghost replay window requires projection acknowledgement")
    if position and _event_position(database, stream, position=position) != 1:
        raise ValueError("Ghost replay checkpoint anchor is missing")
    return position


def acknowledge_window(database: sqlite3.Connection, stream: EventStream) -> None:
    database.execute(
        "UPDATE ghost_replay_windows SET applied_after_sequence=after_sequence WHERE stream=?", (stream,),
    )


def begin_checkpoint(database: sqlite3.Connection, stream: EventStream) -> None:
    """Exclude prior events from replay, atomically with the caller's snapshot."""
    event_window(database, stream)
    database.execute(
        "UPDATE ghost_replay_windows SET after_sequence=? WHERE stream=?",
        (_event_position(database, stream), stream),
    )


def reset_window(database: sqlite3.Connection, stream: EventStream) -> None:
    """Exclude all existing history in an explicit atomic projection reset.

    The caller clears the projection and acknowledges its cursor in the same
    transaction. Unlike ordinary checkpoints, reset can recover damaged window
    metadata by deriving both positions from the original stream's actual tail.
    """
    boundary = _event_position(database, stream)
    database.execute(
        "INSERT INTO ghost_replay_windows(stream,after_sequence,applied_after_sequence) VALUES(?,?,?) "
        "ON CONFLICT(stream) DO UPDATE SET after_sequence=excluded.after_sequence,"
        "applied_after_sequence=excluded.applied_after_sequence",
        (stream, boundary, boundary),
    )


def checkpoint_events(events: Iterable[Mapping[str, object]]) -> Iterator[Mapping[str, object]]:
    """A checkpoint copies state into fresh envelopes without reusing old IDs."""
    identity = uuid.uuid4().hex
    for index, event in enumerate(events):
        yield dict(event, event_id=f"gcp_{identity}_{index}")


def advance_observation_window(database: sqlite3.Connection, *, limit: int) -> int:
    if limit <= 0:
        raise ValueError("observation window requires a positive limit")
    previous = event_window(database, "observations")
    row: sqlite3.Row | None = database.execute(
        "SELECT sequence FROM observation_history ORDER BY sequence DESC LIMIT 1 OFFSET ?", (limit,),
    ).fetchone()
    if row is None:
        return previous
    position: object = row[0]
    if type(position) is not int or position < 0:
        raise ValueError("invalid Ghost observation window position")
    boundary = max(previous, position)
    database.execute(
        "UPDATE ghost_replay_windows SET after_sequence=?,applied_after_sequence=? WHERE stream='observations'",
        (boundary, boundary),
    )
    return boundary
