"""SQLite row projection into durable domain records.

The SQLite store owns transaction ordering and constraint checks.  This module
owns only the mechanical row-to-model boundary so query methods do not each
reimplement JSON decoding and enum conversion.
"""

from __future__ import annotations

import json
import sqlite3

from codey.durable.model import (
    ArtifactRecord,
    EffectRecord,
    EntryDraft,
    SubmissionPlacement,
    SubmissionRecord,
    SubmissionStatus,
    TaskCheckpoint,
    require_checkpoint_version,
)
from codey.types.json_value import require_json_integer, require_json_string
from codey.utils.object_fields import object_fields


def _column(row: sqlite3.Row, key: str) -> object:
    value: object = row[key]
    return value


def _text(row: sqlite3.Row, key: str) -> str:
    return require_json_string(_column(row, key), key)


def _optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = _column(row, key)
    return None if value is None else require_json_string(value, key)


def _integer(row: sqlite3.Row, key: str) -> int:
    return require_json_integer(_column(row, key), key)


def _checked_object(decoded: object, key: str) -> dict[str, object]:
    if not object_fields(decoded):
        raise ValueError(f"{key} must contain a JSON object")
    return decoded


def _object(row: sqlite3.Row, key: str) -> dict[str, object]:
    decoded: object = json.loads(_text(row, key))
    return _checked_object(decoded, key)


def _optional_object(row: sqlite3.Row, key: str) -> dict[str, object] | None:
    value = _optional_text(row, key)
    if value is None:
        return None
    decoded: object = json.loads(value)
    return None if decoded is None else _checked_object(decoded, key)


def entry_from_row(row: sqlite3.Row) -> EntryDraft:
    commit = _column(row, "commit_seq")
    return EntryDraft(
        _text(row, "id"),
        _text(row, "conversation_id"),
        _optional_text(row, "parent_id"),
        _text(row, "kind"),
        _object(row, "payload"),
        0 if commit is None else require_json_integer(commit, "commit_seq"),
        _optional_text(row, "by_task_id"),
    )


def task_from_row(row: sqlite3.Row) -> TaskCheckpoint:
    phase = _text(row, "phase")
    schema_version = require_checkpoint_version(_integer(row, "schema_version"))
    return TaskCheckpoint(
        task_id=_text(row, "id"),
        conversation_id=_text(row, "conversation_id"),
        submission_id=_text(row, "submission_id"),
        phase=phase,
        revision=_integer(row, "revision"),
        provider_snapshot=_object(row, "provider_snapshot"),
        state=_object(row, "state"),
        current_turn_id=_optional_text(row, "current_turn_id"),
        pending_effect_id=_optional_text(row, "pending_effect_id"),
        status=_text(row, "status"),
        schema_version=schema_version,
    )


def effect_from_row(row: sqlite3.Row) -> EffectRecord:
    return EffectRecord(
        effect_id=_text(row, "id"),
        task_id=_text(row, "task_id"),
        category=_text(row, "category"),
        replay_class=_text(row, "replay_class"),
        args_digest=_text(row, "args_digest"),
        status=_text(row, "status"),
        intent=_object(row, "intent"),
        settlement=_optional_object(row, "settlement"),
    )


def artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        handle=_text(row, "handle"),
        sha256=_text(row, "sha256"),
        byte_count=_integer(row, "byte_count"),
        storage_path=_text(row, "storage_path"),
        owner_task_id=_optional_text(row, "owner_task_id"),
        owner_effect_id=_optional_text(row, "owner_effect_id"),
        created_at=_text(row, "created_at"),
        metadata=_object(row, "metadata"),
    )


def submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
    return SubmissionRecord(
        submission_id=_text(row, "id"),
        conversation_id=_text(row, "conversation_id"),
        request_id=_text(row, "request_id"),
        status=SubmissionStatus(_text(row, "status")),
        input_entry_id=_optional_text(row, "entry_id"),
        answer_entry_id=_optional_text(row, "answer_entry_id"),
        reason=_optional_text(row, "reason"),
        detail=_optional_object(row, "detail"),
        placement=SubmissionPlacement(_text(row, "placement")),
        mode=_text(row, "mode"),
        content=_optional_object(row, "content"),
    )


__all__ = [
    "artifact_from_row",
    "effect_from_row",
    "entry_from_row",
    "submission_from_row",
    "task_from_row",
]
