"""Project existing completion checks as observations, without new verdicts."""

from __future__ import annotations

from codey.app.memory_candidates import accepted_completion
from codey.durable.session import DurableSession
from codey.memory.codec import digest
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonValue, decode_json_object, require_json_string


def completion_observations(durable: DurableSession, outputs: ManagedOutputStore | None, run_id: str) -> list[JsonValue]:
    if outputs is None:
        return []
    result, decision_ref = accepted_completion(durable, outputs, run_id)
    proof = result.get("proof") if result else None
    if proof is None:
        return []
    payload = decode_json_object(proof)
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise ValueError("completion proof has invalid checks")
    artifact = durable.artifact(decision_ref)
    from codey.durable.model import thaw_json

    ref = decode_json_object(thaw_json(artifact.metadata.get("ref")))
    decision = decode_json_object(outputs.read_json_artifact(dict[str, object](ref)))
    data = decode_json_object(decision.get("data"))
    revision = data.get("workspace_revision")
    fingerprint = data.get("workspace_fingerprint")
    workspace_revision = str(revision) + ":" + fingerprint if type(revision) is int and isinstance(fingerprint, str) else ""
    result_rows: list[JsonValue] = []
    for value in checks:
        check = decode_json_object(value)
        key = require_json_string(check.get("check_id"), "check_id")
        state = require_json_string(check.get("status"), "status")
        identity = "check:" + digest({"run": run_id, "proof": payload.get("proof_id"), "check": key})
        result_rows.append({"effect_id": identity, "category": "completion_check", "tool_name": "",
            "status": state, "check_key": key, "value": state if state in {"pass", "fail"} else "unknown",
            "proof_ref": payload.get("proof_id"), "workspace_revision": workspace_revision,
            "reason": check.get("reason_code", "")})
    return result_rows
