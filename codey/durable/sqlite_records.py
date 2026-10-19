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
)


def entry_from_row(row: sqlite3.Row) -> EntryDraft:
    return EntryDraft(
        str(row["id"]),
        str(row["conversation_id"]),
        row["parent_id"],
        str(row["kind"]),
        json.loads(str(row["payload"])),
        int(row["commit_seq"]) if row["commit_seq"] is not None else 0,
        row["by_task_id"],
    )


def task_from_row(row: sqlite3.Row) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        submission_id=str(row["submission_id"]),
        phase=str(row["phase"]),
        revision=int(row["revision"]),
        provider_snapshot=json.loads(str(row["provider_snapshot"])),
        state=json.loads(str(row["state"])),
        current_turn_id=row["current_turn_id"],
        pending_effect_id=row["pending_effect_id"],
        status=str(row["status"]),
        schema_version=int(row["schema_version"]),
    )


def effect_from_row(row: sqlite3.Row) -> EffectRecord:
    settlement = row["settlement"]
    return EffectRecord(
        effect_id=str(row["id"]),
        task_id=str(row["task_id"]),
        category=str(row["category"]),
        replay_class=str(row["replay_class"]),
        args_digest=str(row["args_digest"]),
        status=str(row["status"]),
        intent=json.loads(str(row["intent"])),
        settlement=None if settlement is None else json.loads(str(settlement)),
    )


def artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        handle=str(row["handle"]),
        sha256=str(row["sha256"]),
        byte_count=int(row["byte_count"]),
        storage_path=str(row["storage_path"]),
        owner_task_id=None if row["owner_task_id"] is None else str(row["owner_task_id"]),
        owner_effect_id=None if row["owner_effect_id"] is None else str(row["owner_effect_id"]),
        created_at=str(row["created_at"]),
        metadata=json.loads(str(row["metadata"])),
    )


def submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
    detail = row["detail"]
    return SubmissionRecord(
        submission_id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        request_id=str(row["request_id"]),
        status=SubmissionStatus(str(row["status"])),
        input_entry_id=None if row["entry_id"] is None else str(row["entry_id"]),
        answer_entry_id=row["answer_entry_id"],
        reason=row["reason"],
        detail=None if detail is None else json.loads(str(detail)),
        placement=SubmissionPlacement(str(row["placement"])),
        mode=str(row["mode"]),
        content=json.loads(str(row["content"])),
    )


__all__ = [
    "artifact_from_row",
    "effect_from_row",
    "entry_from_row",
    "submission_from_row",
    "task_from_row",
]
