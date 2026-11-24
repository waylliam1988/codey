"""Control configuration and receipts are canonical passive entries.

No sidecar database, synthetic agent tasks, or alternate execution state. Reads
use all entries so selecting an older context branch cannot undo revocation.
Callers enter DurableSession.control_line for compound coordination.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from codey.control.protocol import ControlError, fingerprint
from codey.durable.model import EntryDraft, thaw_json
from codey.utils.object_fields import object_fields, require_object_fields

if TYPE_CHECKING:
    from codey.durable.session import DurableSession

CONFIG_KIND = "control_config"
RECEIPT_KIND = "control_receipt"


def _entry_data(entry: EntryDraft) -> dict[str, object] | None:
    data = thaw_json(entry.payload.get("data"))
    return data if object_fields(data) else None


def _request_fingerprint(actor: str, name: str, params: Mapping[str, object]) -> str:
    identity: dict[str, object] = {"actor": actor, "operation": name, "params": dict(params)}
    return fingerprint(identity)


def latest(session: DurableSession, conversation_id: str) -> dict[str, object] | None:
    if not session.has_conversation(conversation_id):
        return None
    for entry in reversed(session.entries(conversation_id)):
        if entry.kind == CONFIG_KIND:
            data = _entry_data(entry)
            if data is not None:
                return require_object_fields(data["config"])
    return None


def receipt(
    session: DurableSession, conversation_id: str, actor: str, name: str, params: Mapping[str, object],
) -> dict[str, object] | None:
    if not session.has_conversation(conversation_id):
        return None
    expected = _request_fingerprint(actor, name, params)
    for entry in reversed(session.entries(conversation_id)):
        if entry.kind not in {CONFIG_KIND, RECEIPT_KIND}:
            continue
        data = _entry_data(entry)
        if data is None:
            continue
        if data.get("request_id") != params["request_id"]:
            continue
        if data.get("fingerprint") != expected:
            raise ControlError("REQUEST_CONFLICT", "This request ID already names different content")
        return require_object_fields(data["result"])
    return None


def write(
    session: DurableSession, conversation_id: str, actor: str, name: str, params: Mapping[str, object],
    result: Mapping[str, object], *, config: Mapping[str, object] | None = None,
) -> None:
    data: dict[str, object] = {
        "request_id": params["request_id"], "fingerprint": _request_fingerprint(actor, name, params),
        "result": dict(result),
    }
    if config is not None:
        data["config"] = config
    session.admit_write(conversation_id=conversation_id, request_id="control:" + str(params["request_id"]),
                        kind=CONFIG_KIND if config is not None else RECEIPT_KIND, data=data)
