"""Lossless, bounded receipts for model-visible tool results.

The session log pins the receipt digest. Large receipts use the existing
managed store; source bodies and command artifacts remain separate references.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectSettlement, compute_args_digest

INLINE_RECEIPT_CHARS = 8000


def tool_history_messages(settlement: RuntimeEffectSettlement) -> tuple[tuple[str, dict[str, object]], ...]:
    """Persist bounded native history in the same commit as its receipt."""
    if not settlement.call_id:
        return ()
    return (("tool_result", {
        "call_id": settlement.call_id,
        "content": settlement.result_text or settlement.result_excerpt,
        "is_error": settlement.status != "ok",
        "result_ref": settlement.result_ref,
    }),)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def result_receipt_fields(result: ToolResult, *, store: Any,
                          session_id: str, run_id: str, effect_id: str) -> dict[str, Any]:
    from codey.operations.kernel_provenance import _kernel_workspace_identity_of

    identity = _kernel_workspace_identity_of(result)
    data = {
        "schema_version": 2,
        "call_name": result.call.name,
        "ok": result.ok,
        "args": result.call.args,
        "model_text": result.model_text,
        "canonical": result.canonical,
        "presentation": result.presentation,
        "audit": result.audit,
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


def restore_result_receipt(projection: Any, *, store: Any,
                           session_id: str, run_id: str, project_path: Any = None,
                           workspace_store: Any = None, ignored_paths: tuple[str, ...] = ()) -> ToolResult:
    settlement, intent = projection.settlement, projection.intent
    if settlement is None:
        raise ValueError("effect has no settlement")
    payload = settlement.result_payload
    if settlement.result_ref:
        if payload or store is None:
            raise ValueError("invalid managed result receipt")
        data = store.read_json_artifact({"session_id": session_id, "run_id": run_id,
            "handle": settlement.result_ref, "schema_version": 2, "sha256": settlement.result_payload_sha256})
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if not payload or text_digest(payload) != settlement.result_payload_sha256:
        raise ValueError("tool result receipt digest mismatch")
    data = json.loads(payload)
    if not isinstance(data, dict) or set(data) != {
        "schema_version", "call_name", "ok", "args", "model_text", "canonical", "presentation", "audit", "truncated", "workspace_identity",
    }:
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
    artifact = data["audit"].get("managed_output")
    if artifact:
        if not isinstance(artifact, dict) or store is None:
            raise ValueError("cannot verify tool output artifact")
        _, metadata = store.read_tool_output(session_id, run_id, artifact["handle"])
        if artifact.get("sha256") != metadata.get("sha256"):
            raise ValueError("tool output artifact differs from receipt")
    result = ToolResult(
        ok=data["ok"], call=ToolCall(data["call_name"], data["args"], call_id=intent.call_id),
        model_text=text, canonical=data["canonical"], presentation=data["presentation"],
        audit=data["audit"], truncated=data["truncated"],
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


def _validate_call_name(data: dict[str, Any], tool_name: str) -> None:
    from codey.operations.task_effects import _runtime_name
    if (type(data["schema_version"]) is not int or data["schema_version"] != 2
            or not isinstance(data["call_name"], str) or not data["call_name"]
            or _runtime_name(data["call_name"]) != tool_name):
        raise ValueError("tool result original call name differs from intent")
