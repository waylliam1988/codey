"""Control configuration and receipts are canonical passive entries.

No sidecar database, synthetic agent tasks, or alternate execution state. Reads
use all entries so selecting an older context branch cannot undo revocation.
Callers enter DurableSession.control_line for compound coordination.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from codey.control.protocol import ControlError, fingerprint
from codey.durable.model import thaw_json

CONFIG_KIND = "control_config"
RECEIPT_KIND = "control_receipt"


def latest(session: Any, conversation_id: str) -> dict[str, Any] | None:
    if not session.has_conversation(conversation_id):
        return None
    for entry in reversed(session.entries(conversation_id)):
        if entry.kind == CONFIG_KIND:
            payload = thaw_json(entry.payload)
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if isinstance(data, dict):
                return dict(data["config"])
    return None


def receipt(session: Any, conversation_id: str, actor: str, name: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if not session.has_conversation(conversation_id):
        return None
    expected = fingerprint({"actor": actor, "operation": name, "params": params})
    for entry in reversed(session.entries(conversation_id)):
        payload = thaw_json(entry.payload)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if entry.kind not in {CONFIG_KIND, RECEIPT_KIND} or not isinstance(data, Mapping):
            continue
        if data.get("request_id") != params["request_id"]:
            continue
        if data.get("fingerprint") != expected:
            raise ControlError("REQUEST_CONFLICT", "This request ID already names different content")
        return dict(data["result"])
    return None


def write(session: Any, conversation_id: str, actor: str, name: str, params: dict[str, Any],
          result: dict[str, Any], *, config: dict[str, Any] | None = None) -> None:
    data = {
        "request_id": params["request_id"], "fingerprint": fingerprint({"actor": actor, "operation": name, "params": params}),
        "result": result,
    }
    if config is not None:
        data["config"] = config
    session.admit_write(conversation_id=conversation_id, request_id="control:" + str(params["request_id"]),
                        kind=CONFIG_KIND if config is not None else RECEIPT_KIND, data=data)
