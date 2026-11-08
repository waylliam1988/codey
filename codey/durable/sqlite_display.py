"""Transactional persistence of the shared display projection."""

from __future__ import annotations

import sqlite3

from codey.durable.history_index import DisplayNode, DisplayVersion, NodeBatch, extend, rewrites_path
from codey.durable.model import DurableMutation, EntryDraft


class SQLiteDisplayIndex:
    def __init__(self, database: sqlite3.Connection) -> None:
        self.database = database
        self._nodes: dict[str, DisplayNode] = {}

    def node(self, identity: str) -> DisplayNode:
        cached = self._nodes.get(identity)
        if cached is not None:
            return cached
        row = self.database.execute("SELECT * FROM display_nodes WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("missing display index node")
        node = DisplayNode(str(row["node_key"]), str(row["value"]), bool(row["question"]),
                           row["left_id"], row["right_id"], int(row["height"]), int(row["count"]), int(row["questions"]))
        self._nodes[identity] = node
        return node

    def version(self, entry_id: str | None) -> DisplayVersion:
        if entry_id is None:
            return DisplayVersion()
        row = self.database.execute("SELECT * FROM display_versions WHERE entry_id=?", (entry_id,)).fetchone()
        if row is None:
            raise KeyError("missing display history version")
        return DisplayVersion(row["order_root"], row["locations_root"], row["runs_root"], str(row["cut_id"]))


def write_display_index(database: sqlite3.Connection, mutation: DurableMutation, entries: tuple[EntryDraft, ...]) -> None:
    if rewrites_path(mutation):
        database.execute("UPDATE conversations SET path_generation=path_generation+1 WHERE id=?",
                         (mutation.conversation_id,))
    if not entries:
        return
    index = SQLiteDisplayIndex(database)
    sequence = int(database.execute("SELECT value FROM schema_metadata WHERE key='display_node_sequence'").fetchone()[0])

    def allocate() -> str:
        nonlocal sequence
        sequence += 1
        return str(sequence)

    batch = NodeBatch(index, allocate)
    versions: dict[str, DisplayVersion] = {}
    inherited = database.execute("SELECT parent_entry_id FROM conversations WHERE id=?",
                                 (mutation.conversation_id,)).fetchone()[0]
    for entry in entries:
        base = entry.parent_id or inherited
        if entry.payload.get("edited_entry_id"):
            bridge = entry.payload.get("edit_parent_entry_id")
            base = bridge if isinstance(bridge, str) else None
        previous = versions.get(base) if base else None
        version = extend(batch, previous if previous is not None else index.version(base), entry)
        versions[entry.entry_id] = version
        database.execute(
            "INSERT INTO display_versions VALUES(?,?,?,?,?,?)",
            (entry.entry_id, version.order, version.locations, version.runs, version.cut,
             str(entry.payload.get("text") or "")[:240] if entry.kind == "user_message" else ""),
        )
    database.executemany(
        "INSERT INTO display_nodes VALUES(?,?,?,?,?,?,?,?,?,?)",
        ((identity, mutation.conversation_id, node.key, node.value, int(node.question), node.left, node.right,
          node.height, node.count, node.questions) for identity, node in batch.reachable(list(versions.values())).items()),
    )
    database.execute("UPDATE schema_metadata SET value=? WHERE key='display_node_sequence'", (str(sequence),))
