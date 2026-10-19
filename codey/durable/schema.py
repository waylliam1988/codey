"""SQLite schema declarations and strict cold-start shape validation."""

from __future__ import annotations

import sqlite3

from codey.durable.errors import SchemaVersionError

SCHEMA_VERSION = 1

# The current durable schema is installed atomically on a fresh database. A
# future schema may add migration 2, but this cold-start build deliberately
# carries no historical upgrade code or compatibility writer.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """CREATE TABLE commits (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                payload TEXT NOT NULL CHECK(json_valid(payload))
            ) STRICT""",
            """CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                head_entry_id TEXT,
                revision INTEGER NOT NULL CHECK(revision >= 0),
                parent_conversation_id TEXT,
                parent_entry_id TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            ) STRICT""",
            """CREATE TABLE entries (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                parent_id TEXT,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL CHECK(json_valid(payload)),
                ordinal INTEGER NOT NULL,
                commit_seq INTEGER NOT NULL REFERENCES commits(seq),
                by_task_id TEXT,
                UNIQUE(conversation_id, ordinal),
                UNIQUE(conversation_id, id),
                FOREIGN KEY(conversation_id, parent_id) REFERENCES entries(conversation_id, id)
            ) STRICT""",
            """CREATE TABLE submissions (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                request_id TEXT NOT NULL,
                entry_id TEXT REFERENCES entries(id),
                revision INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                placement TEXT NOT NULL DEFAULT 'placed',
                mode TEXT NOT NULL DEFAULT 'follow_up',
                content TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(content)),
                commit_seq INTEGER NOT NULL REFERENCES commits(seq),
                answer_entry_id TEXT,
                reason TEXT,
                detail TEXT CHECK(detail IS NULL OR json_valid(detail)),
                UNIQUE(conversation_id, request_id)
            ) STRICT""",
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                submission_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 0),
                provider_snapshot TEXT NOT NULL CHECK(json_valid(provider_snapshot)),
                state TEXT NOT NULL CHECK(json_valid(state)),
                current_turn_id TEXT,
                pending_effect_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                schema_version INTEGER NOT NULL DEFAULT 1
            ) STRICT""",
            """CREATE TABLE finalization_queue (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE task_waiters (
                waited_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                waiter_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                PRIMARY KEY(waited_task_id, waiter_task_id),
                CHECK(waited_task_id <> waiter_task_id)
            ) STRICT""",
            """CREATE TABLE scheduler_owners (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                owner_id TEXT NOT NULL,
                process_id INTEGER NOT NULL CHECK(process_id > 0),
                claimed_at TEXT NOT NULL
            ) STRICT""",
            """CREATE TABLE task_owners (
                owner_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                child_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                PRIMARY KEY(owner_task_id, child_task_id),
                CHECK(owner_task_id <> child_task_id)
            ) STRICT""",
            """CREATE TABLE effects (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                category TEXT NOT NULL,
                replay_class TEXT NOT NULL,
                args_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                intent TEXT NOT NULL CHECK(json_valid(intent)),
                settlement TEXT CHECK(settlement IS NULL OR json_valid(settlement))
            ) STRICT""",
            """CREATE TABLE artifacts (
                handle TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
                storage_path TEXT NOT NULL,
                owner_task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                owner_effect_id TEXT REFERENCES effects(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL CHECK(json_valid(metadata)),
                CHECK(owner_task_id IS NOT NULL OR owner_effect_id IS NOT NULL)
            ) STRICT""",
            "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
            "CREATE INDEX entries_by_conversation_parent ON entries(conversation_id, parent_id)",
            "CREATE INDEX entries_by_task ON entries(by_task_id)",
            "CREATE INDEX tasks_by_phase ON tasks(phase)",
            "CREATE INDEX tasks_by_status ON tasks(status)",
            "CREATE INDEX tasks_by_checkpoint_schema ON tasks(schema_version)",
            "CREATE INDEX finalization_queue_by_task ON finalization_queue(task_id)",
            "CREATE INDEX task_waiters_by_waited ON task_waiters(waited_task_id)",
            "CREATE INDEX task_waiters_by_waiter ON task_waiters(waiter_task_id)",
            "CREATE INDEX task_owners_by_owner ON task_owners(owner_task_id)",
            "CREATE INDEX task_owners_by_child ON task_owners(child_task_id)",
            "CREATE INDEX effects_by_task_digest ON effects(task_id, args_digest)",
            "CREATE INDEX artifacts_by_task ON artifacts(owner_task_id)",
            "CREATE INDEX artifacts_by_effect ON artifacts(owner_effect_id)",
        ),
    ),
)


def validate_schema(database: sqlite3.Connection) -> None:
    """Reject incomplete schemas even when their version markers look valid."""
    definitions = {
        str(row["name"]): " ".join(str(row["sql"]).split())
        for row in database.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
    }
    for _version, statements in MIGRATIONS:
        for statement in statements:
            name = statement.split()[2]
            if definitions.get(name) != " ".join(statement.split()):
                raise SchemaVersionError(f"durable schema definition differs for {name}")
