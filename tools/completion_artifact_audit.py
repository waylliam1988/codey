"""Read committed production artifacts without changing a run or its results."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from codey.operations.kernel_receipts import restore_result_receipt
from codey.runtime.core.models import ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectProjection, RuntimeEffectSettlement
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string


@dataclass(frozen=True)
class EffectReceipt:
    identity: str
    result: ToolResult
    exit_code: int | None


def tool_receipts(state: Path) -> list[EffectReceipt]:
    receipts: list[EffectReceipt] = []
    store = ManagedOutputStore(state)
    with sqlite3.connect((state / "durable.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as connection:
        for row in connection.execute("SELECT intent, settlement FROM effects WHERE category='tool_call' "
                                      "AND settlement IS NOT NULL ORDER BY rowid"):
            raw_intent: object = row[0]
            raw_settlement: object = row[1]
            if not isinstance(raw_intent, str) or not isinstance(raw_settlement, str):
                raise ValueError("invalid stored effect")
            intent = RuntimeEffectIntent.from_payload(json.loads(raw_intent))
            settlement = RuntimeEffectSettlement.from_payload(json.loads(raw_settlement))
            result = restore_result_receipt(RuntimeEffectProjection(intent=intent, settlement=settlement), store=store,
                session_id=intent.session_id, run_id=intent.run_id)
            receipts.append(EffectReceipt(intent.effect_id, result, settlement.exit_code))
    return receipts


def artifacts(state: Path) -> list[tuple[str, JsonObject]]:
    store = ManagedOutputStore(state)
    rows: list[tuple[str, JsonObject]] = []
    with sqlite3.connect((state / "durable.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as connection:
        for raw in connection.execute("SELECT metadata FROM artifacts ORDER BY rowid"):
            raw_metadata: object = raw[0]
            if not isinstance(raw_metadata, str):
                raise ValueError("invalid artifact metadata")
            metadata = decode_json_object(json.loads(raw_metadata))
            raw_reference = metadata.get("ref")
            if not isinstance(raw_reference, dict):
                continue
            reference = decode_json_object(raw_reference)
            body = decode_json_object(store.read_json_artifact(dict(reference)))
            rows.append((require_json_string(reference["handle"], "handle"), body))
    return rows


def rejection_audit(state: Path) -> JsonObject:
    if not (state / "durable.sqlite3").exists():
        return {"first_rejection": None, "repair_attempts": []}
    ordered = artifacts(state)
    by_handle = dict(ordered)
    attempts: list[JsonValue] = []
    for handle, body in ordered:
        if body.get("kind") != "provider_request":
            continue
        request = decode_json_object(body["payload"])
        if request.get("delivery_purpose") != "retry":
            continue
        decision_ref = require_json_string(request["work_ref"], "work_ref")
        response_ref = require_json_string(request["source_response_ref"], "source_response_ref")
        decision = decode_json_object(by_handle[decision_ref]["data"])
        attempts.append({"request_ref": handle, "decision_ref": decision_ref,
                         "reason": decision["prompt"], "proposal_ref": response_ref,
                         "proposal": by_handle[response_ref]["payload"]})
    return {"first_rejection": attempts[0] if attempts else None, "repair_attempts": attempts}
