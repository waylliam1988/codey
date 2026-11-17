"""Application admission receipts in the original canonical input commit.

Receipts describe optional consumers, never task execution or authorization.
The reader opens a short read-only connection; it cannot initialize or mutate
the store and does not reconstruct a receipt from today's mutable checkpoint.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from codey.durable.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class InputReceipt:
    component: str
    accepted: bool

    def __post_init__(self) -> None:
        if not self.component or type(self.accepted) is not bool:
            raise ValueError("an input receipt requires a component and a boolean result")


def receipt_fields(content: Mapping[str, object] | None) -> dict[str, bool]:
    raw = (content or {}).get("input_receipts", {})
    if not isinstance(raw, Mapping):
        raise ValueError("input receipts must be an object")
    fields: Mapping[object, object] = raw
    result: dict[str, bool] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not key or type(value) is not bool:
            raise ValueError("invalid input receipt")
        result[key] = value
    return result


def read_input_receipt(path: Path, task_id: str, component: str) -> bool | None:
    """Read the original receipt, or None for an input without that receipt.

    Storage/schema errors propagate; consumers decide their failure policy.
    Opening with mode=ro also prevents an absent authority from being created.
    """
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as database:
        version: tuple[object, ...] | None = database.execute("PRAGMA user_version").fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            raise ValueError("unsupported canonical input receipt schema")
        row: tuple[object, ...] | None = database.execute(
            "SELECT s.id,json_extract(c.payload,'$.projections') FROM tasks AS t "
            "JOIN submissions AS s ON s.id=t.submission_id AND s.conversation_id=t.conversation_id "
            "JOIN commits AS c ON c.seq=s.commit_seq WHERE t.id=?", (task_id,),
        ).fetchone()
    if row is None:
        return None
    submission_id, encoded = row
    if not isinstance(submission_id, str) or not isinstance(encoded, str):
        raise ValueError("invalid canonical receipt row")
    projections: object = json.loads(encoded)
    if not isinstance(projections, (tuple, list)):
        raise ValueError("invalid canonical receipt projections")
    rows: Sequence[object] = projections
    selected: dict[str, bool] | None = None
    for item in rows:
        if not isinstance(item, Mapping):
            raise ValueError("invalid canonical receipt projection")
        projection: Mapping[object, object] = item
        if projection.get("type") != "submission_admitted" or projection.get("submission_id") != submission_id:
            continue
        if selected is not None:
            raise ValueError("duplicate canonical input receipt")
        selected = receipt_fields({"input_receipts": projection.get("input_receipts", {})})
    return None if selected is None else selected.get(component)
