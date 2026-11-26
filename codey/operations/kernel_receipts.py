"""Lossless, bounded receipts for model-visible tool results.

The session log pins the receipt digest. Large receipts use the existing
managed store; source bodies and command artifacts remain separate references.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import (
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    compute_args_digest,
)
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.tool_names import runtime_tool_name
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    parse_json_object,
    require_json_boolean,
    require_json_string,
)
from codey.types.tool_image import TOOL_IMAGES_KEY, ToolImageRef
from codey.workspace.revision import WorkspaceRevisionStore

INLINE_RECEIPT_CHARS = 8000


class ResultReceiptFields(TypedDict):
    result_payload: str
    result_payload_sha256: str
    result_ref: str
    result_excerpt: str
    result_text: str
    result_len: int
    result_sha256: str
    result_truncated: bool
    call_id: str


def tool_history_messages(settlement: RuntimeEffectSettlement, *, images: tuple[ToolImageRef, ...] = ()) -> tuple[tuple[str, dict[str, object]], ...]:
    """Persist bounded native history in the same commit as its receipt."""
    if not settlement.call_id:
        return ()
    return (("tool_result", {
        "call_id": settlement.call_id,
        "content": settlement.result_text or settlement.result_excerpt,
        "is_error": settlement.status != "ok",
        "result_ref": settlement.result_ref,
        **({TOOL_IMAGES_KEY: [image.payload() for image in images]} if images else {}),
    }),)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def result_receipt_fields(result: ToolResult, *, store: ManagedOutputStore | None,
                          session_id: str, run_id: str, effect_id: str) -> ResultReceiptFields:
    from codey.operations.kernel_provenance import _kernel_workspace_identity_of

    identity = _kernel_workspace_identity_of(result)
    data = {
        "schema_version": 3,
        "kind": result.kind,
        "call_name": result.call.name,
        "ok": result.ok,
        "args": result.call.args,
        "model_text": result.model_text,
        "canonical": result.canonical,
        "presentation": result.presentation,
        "audit": result.audit,
        "images": [image.payload() for image in result.images],
        "truncated": result.truncated,
        "workspace_identity": {"workspace_revision": identity.revision,
                               "workspace_fingerprint": identity.fingerprint} if identity is not None else None,
    }
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    inline, ref = payload, ""
    if len(payload) > INLINE_RECEIPT_CHARS:
        if store is None:
            raise ValueError("tool result requires a durable managed receipt")
        # Required execution records use the existing lossless JSON artifacts;
        # optional output attachments have a separate, bounded handle quota.
        receipt = store.write_json_artifact(session_id=session_id, run_id=run_id, payload=data, schema_version=2)
        if receipt is None or receipt.sha256 != text_digest(payload):
            raise ValueError("tool result receipt could not be stored completely")
        inline, ref = "", receipt.handle
    text = result.model_text
    return {
        "result_payload": inline,
        "result_payload_sha256": text_digest(payload),
        "result_ref": ref,
        "result_excerpt": text[:500],
        "result_text": text if len(text) <= INLINE_RECEIPT_CHARS else "",
        "result_len": len(text),
        "result_sha256": text_digest(text),
        # This describes the original observation, not storage clipping.
        "result_truncated": result.truncated,
        "call_id": result.call.call_id,
    }


def _managed_receipt_payload(settlement: RuntimeEffectSettlement,
                             store: ManagedOutputStore, session_id: str, run_id: str) -> str:
    if not settlement.result_ref.startswith("artifact_"):
        raise ValueError("unknown managed result receipt format")
    data = store.read_json_artifact({"session_id": session_id, "run_id": run_id,
        "handle": settlement.result_ref, "schema_version": 2, "sha256": settlement.result_payload_sha256})
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def restore_result_receipt(projection: RuntimeEffectProjection, *, store: ManagedOutputStore | None,
                           session_id: str, run_id: str, project_path: Path | None = None,
                           workspace_store: WorkspaceRevisionStore | None = None,
                           ignored_paths: tuple[str, ...] = ()) -> ToolResult:
    settlement, intent = projection.settlement, projection.intent
    if settlement is None:
        raise ValueError("effect has no settlement")
    payload = settlement.result_payload
    if settlement.result_ref:
        if payload or store is None:
            raise ValueError("invalid managed result receipt")
        payload = _managed_receipt_payload(settlement, store, session_id, run_id)
    if not payload or text_digest(payload) != settlement.result_payload_sha256:
        raise ValueError("tool result receipt digest mismatch")
    data = parse_json_object(payload)
    expected = {
        "schema_version", "kind", "call_name", "ok", "args", "model_text", "canonical", "presentation", "audit", "truncated", "workspace_identity",
    }
    if set(data) != expected | {"images"}:
        raise ValueError("invalid tool result receipt fields")
    _validate_call_name(data, intent.tool_name)
    if type(data["ok"]) is not bool or data["ok"] != (settlement.status == "ok"):
        raise ValueError("tool result status differs from settlement")
    if not isinstance(data["args"], dict) or compute_args_digest(data["args"]) != intent.args_digest:
        raise ValueError("tool result arguments differ from intent")
    text = data["model_text"]
    if (not isinstance(text, str) or len(text) != settlement.result_len
            or text_digest(text) != settlement.result_sha256):
        raise ValueError("tool result text differs from settlement")
    if type(data["truncated"]) is not bool or data["truncated"] != settlement.result_truncated:
        raise ValueError("tool result truncation differs from settlement")
    if settlement.call_id != intent.call_id:
        raise ValueError("tool result call identity differs from intent")
    for key in ("canonical", "presentation", "audit"):
        if not isinstance(data[key], dict):
            raise ValueError(f"invalid tool result {key}")
    # Verify the full receipt here. External output is checked by the output
    # resolver when read; losing an attachment cannot erase known execution facts.
    images = data["images"]
    if not isinstance(images, list):
        raise ValueError("invalid tool result images")
    result = ToolResult(
        ok=require_json_boolean(data["ok"], "ok"),
        kind=require_json_string(data["kind"], "kind"),
        call=ToolCall(require_json_string(data["call_name"], "call_name"), decode_json_object(data["args"]), call_id=intent.call_id),
        model_text=text, canonical=decode_json_object(data["canonical"]), presentation=decode_json_object(data["presentation"]),
        audit=decode_json_object(data["audit"]), truncated=require_json_boolean(data["truncated"], "truncated"),
        images=tuple(ToolImageRef.read(image) for image in images),
    )
    if data["workspace_identity"] is not None:
        from codey.operations.kernel_provenance import _trusted_workspace_proof, attach_trusted_workspace
        from codey.operations.kernel_recovery_context import verified_persisted_identity

        if not isinstance(data["workspace_identity"], dict):
            raise ValueError("invalid persisted workspace identity")
        identity = verified_persisted_identity(
            data["workspace_identity"], project_path=project_path,
            revision_store=workspace_store, ignored_paths=ignored_paths,
        )
        if identity is not None:
            result = attach_trusted_workspace(result, _trusted_workspace_proof(identity, "persisted_revision_store"))
    return result


def _validate_call_name(data: JsonObject, tool_name: str) -> None:
    if (type(data["schema_version"]) is not int or data["schema_version"] != 3
            or not isinstance(data["call_name"], str) or not data["call_name"]
            or runtime_tool_name(data["call_name"]) != tool_name):
        raise ValueError("tool result original call name differs from intent")
