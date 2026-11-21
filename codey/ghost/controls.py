"""Read the sole journal-derived control projection."""

import sqlite3

from codey.ghost.storage import rows
from codey.memory.contracts import MEMORY_CONTROLS, MemoryPolicy
from codey.types.json_value import require_json_string


def policy(connection: sqlite3.Connection, scopes: tuple[str, ...]) -> MemoryPolicy:
    denied: set[str] = set()
    for row in rows(connection, "controls"):
        if row.get("scope") in scopes and row.get("enabled") is False:
            denied.add(require_json_string(row.get("name"), "name"))
    return MemoryPolicy(*(name not in denied for name in MEMORY_CONTROLS))
