"""Bounded reads against immutable display versions and one database snapshot."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import cast

from codey.durable.history import HistoryReader
from codey.durable.history_index import find, locate, size, window
from codey.durable.session_kernel import SessionKernel
from codey.durable.task_projection import task_lifecycle


@dataclass(frozen=True)
class PagePosition:
    anchor: str | None
    revision: int
    generation: int
    boundary: int


def _cursor(reader: HistoryReader, database_id: str, conversation_id: str, kind: str,
            position: PagePosition) -> str:
    raw = json.dumps([database_id, conversation_id, kind, position.generation, position.anchor,
                      position.revision, position.boundary], separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return payload + "." + hmac.new(reader.cursor_key(), raw, hashlib.sha256).hexdigest()


def _position(reader: HistoryReader, database_id: str, conversation_id: str, kind: str,
              before: str | None) -> PagePosition:
    anchor, revision, generation = reader.display_head(conversation_id)
    if before is None:
        return PagePosition(anchor, revision, generation, size(reader, reader.version(anchor).order,
                                                              questions=kind == "questions"))
    try:
        payload, signature = before.split(".")
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        expected = hmac.new(reader.cursor_key(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        decoded: object = json.loads(raw)
        if not isinstance(decoded, list) or len(decoded) != 7:
            raise ValueError
        fields = cast(list[object], decoded)
        epoch, scope, query, pinned_generation, pinned_anchor, pinned_revision, boundary = fields
        if (epoch != database_id or scope != conversation_id or query != kind
                or type(pinned_generation) is not int or type(pinned_revision) is not int
                or type(boundary) is not int or boundary < 0 or pinned_revision < 0
                or (pinned_anchor is not None and not isinstance(pinned_anchor, str))):
            raise ValueError
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError("invalid conversation cursor") from exc
    if pinned_generation != generation:
        raise ValueError("history snapshot changed")
    version = reader.version(pinned_anchor)
    if boundary > size(reader, version.order, questions=kind == "questions"):
        raise ValueError("invalid conversation cursor")
    return PagePosition(pinned_anchor, pinned_revision, generation, boundary)


def _envelope(database_id: str, conversation_id: str, position: PagePosition) -> dict[str, object]:
    return {"schema_version": 1, "session_id": conversation_id, "epoch": database_id,
            "snapshot_revision": position.revision, "path_generation": position.generation,
            "display_version": position.anchor}


def _body_page(session: SessionKernel, reader: HistoryReader, conversation_id: str,
               position: PagePosition, start: int, end: int) -> dict[str, object]:
    from codey.durable.conversation_ops import _history_item, _resolve_response_entries

    version = reader.version(position.anchor)
    identities = [node.value for node in window(reader, version.order, start, end)]
    selected = _resolve_response_entries(session, reader.select_entries(reader.display_entries(identities)),
                                         artifact=reader.artifact)
    cursor = _cursor(reader, session.database_id, conversation_id, "messages",
                     PagePosition(position.anchor, position.revision, position.generation, start)) if start else None
    return {
        **_envelope(session.database_id, conversation_id, position),
        "items": [_history_item(entry, start + index + 1) for index, entry in enumerate(selected)],
        "task_states": [task_lifecycle(reader.task(run))
                        for run in sorted({entry.by_task_id for entry in selected if entry.by_task_id})],
        "before_cursor": cursor, "has_more": start > 0,
    }


def history_page(session: SessionKernel, conversation_id: str, *, before: str | None,
                 limit: int) -> dict[str, object]:
    with session.history_read(conversation_id) as reader:
        position = _position(reader, session.database_id, conversation_id, "messages", before)
        return _body_page(session, reader, conversation_id, position,
                          max(0, position.boundary - max(1, min(200, int(limit)))), position.boundary)


def navigation_page(session: SessionKernel, conversation_id: str, *, before: str | None,
                    limit: int) -> dict[str, object]:
    with session.history_read(conversation_id) as reader:
        position = _position(reader, session.database_id, conversation_id, "questions", before)
        start = max(0, position.boundary - max(1, min(200, int(limit))))
        version = reader.version(position.anchor)
        identities = [node.value for node in window(reader, version.order, start, position.boundary, questions=True)]
        return {**_envelope(session.database_id, conversation_id, position),
                "items": reader.navigation_items(identities), "has_more": start > 0,
                "before_cursor": _cursor(reader, session.database_id, conversation_id, "questions",
                                          PagePosition(position.anchor, position.revision, position.generation, start))
                if start else None}


def history_window(session: SessionKernel, conversation_id: str, entry_id: str, *, limit: int,
                   leaf_id: str | None) -> dict[str, object]:
    with session.history_read(conversation_id) as reader:
        position = _position(reader, session.database_id, conversation_id, "messages", None)
        if leaf_id is not None:
            # Retained local tips and inherited displayed prefixes are valid;
            # an unrelated conversation's version must never be accepted.
            metadata = reader.display_entries([leaf_id])
            inherited = reader.conversation_parent(conversation_id)[1]
            if (metadata[0].conversation_id != conversation_id and leaf_id != inherited
                    and find(reader, reader.version(inherited).locations, leaf_id) is None):
                raise KeyError("entry is not on this conversation path")
            position = PagePosition(leaf_id, position.revision, position.generation,
                                    size(reader, reader.version(leaf_id).order))
        version = reader.version(position.anchor)
        location = locate(reader, version, entry_id)
        if location is None:
            raise KeyError("entry is not on this conversation path")
        count = max(1, min(200, int(limit)))
        start = max(0, location - count // 2)
        end = min(position.boundary, start + count)
        page = _body_page(session, reader, conversation_id, position, start, end)
        page["has_after"] = end < position.boundary
        return page


def validate_runs(session: SessionKernel, conversation_id: str, run_ids: list[str], *, epoch: str,
                  generation: int) -> dict[str, object]:
    if len(run_ids) > 200 or any(not run or len(run) > 256 for run in run_ids):
        raise ValueError("expected at most 200 task identities")
    with session.history_read(conversation_id) as reader:
        anchor, revision, current_generation = reader.display_head(conversation_id)
        if epoch != session.database_id or generation != current_generation:
            raise ValueError("history snapshot changed")
        version = reader.version(anchor)
        return {**_envelope(session.database_id, conversation_id,
                            PagePosition(anchor, revision, current_generation, 0)),
                "runs": [{"run_id": run, "visible": find(reader, version.runs, run) is not None}
                         for run in dict.fromkeys(run_ids)]}
