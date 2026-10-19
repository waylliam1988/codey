"""Typed durable inbox snapshot and append-only history projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.durable.model import SubmissionPlacement, SubmissionStatus, thaw_json


@dataclass(frozen=True)
class InboxItem:
    submission_id: str
    conversation_id: str
    request_id: str
    status: SubmissionStatus
    placement: SubmissionPlacement
    mode: str
    content: Mapping[str, object] | None
    input_entry_id: str | None
    task_id: str | None


@dataclass(frozen=True)
class InboxDoc:
    conversation_id: str
    revision: int
    items: tuple[InboxItem, ...]

    @property
    def snapshot(self) -> dict[str, object]:
        items = [
            {
                "submission_id": item.submission_id,
                "conversation_id": item.conversation_id,
                "request_id": item.request_id,
                "status": item.status.value,
                "placement": item.placement.value,
                "mode": item.mode,
                "content": thaw_json(item.content) if item.content is not None else None,
                "input_entry_id": item.input_entry_id,
                "task_id": item.task_id,
            }
            for item in self.items
        ]
        payload: dict[str, object] = {
            "schema_version": 1,
            "conversation_id": self.conversation_id,
            "revision": self.revision,
            "items": items,
        }
        payload["digest"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload


@dataclass(frozen=True)
class InboxHistoryEvent:
    sequence: int
    kind: str
    conversation_id: str
    submission_id: str
    request_id: str
    status: SubmissionStatus | None = None
    placement: SubmissionPlacement | None = None


def item_from_record(record: Any, *, task_id: str | None = None) -> InboxItem:
    return InboxItem(
        submission_id=record.submission_id,
        conversation_id=record.conversation_id,
        request_id=record.request_id,
        status=SubmissionStatus(record.status),
        placement=SubmissionPlacement(record.placement),
        mode=record.mode,
        content=record.content,
        input_entry_id=record.input_entry_id,
        task_id=task_id,
    )


def history_event_from_projection(
    sequence: int,
    conversation_id: str,
    projection: Mapping[str, object],
) -> InboxHistoryEvent | None:
    kind = projection.get("type")
    if kind not in {"submission_admitted", "submission_progressed"}:
        return None
    submission_id = projection.get("submission_id")
    request_id = projection.get("request_id")
    if not isinstance(submission_id, str):
        return None
    if not isinstance(request_id, str):
        request_id = ""
    raw_status = projection.get("status")
    raw_placement = projection.get("placement")
    status = SubmissionStatus(raw_status) if isinstance(raw_status, str) else None
    placement = SubmissionPlacement(raw_placement) if isinstance(raw_placement, str) else None
    return InboxHistoryEvent(
        sequence=sequence,
        kind=str(kind),
        conversation_id=conversation_id,
        submission_id=submission_id,
        request_id=request_id,
        status=status,
        placement=placement,
    )
