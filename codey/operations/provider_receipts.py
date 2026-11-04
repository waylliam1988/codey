"""Provider request/response artifacts in the existing managed output store.

The body has one owner. Checkpoints and durable artifact rows carry references;
conversation history is produced separately at its generation boundary.
"""
from __future__ import annotations

import hashlib
from dataclasses import fields
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from codey.durable.model import ArtifactRecord, thaw_json
from codey.providers.response_record import json_value
from codey.runtime.effects.effect_records import RuntimeEffectError
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.context_admission import GhostContextSource

REQUEST_KIND = "provider_request"
RESPONSE_KIND = "provider_response"


def restore_ghost_context(payload: object) -> tuple[str, tuple[GhostContextSource, ...]]:
    if (not isinstance(payload, dict) or set(payload) != {"text", "ghost_sources"}
            or not isinstance(payload["text"], str) or not isinstance(payload["ghost_sources"], list)):
        raise ValueError("frozen request has no valid context snapshot")
    sources = []
    expected = {field.name for field in fields(GhostContextSource)}
    for row in payload["ghost_sources"]:
        if (not isinstance(row, dict) or set(row) != expected or type(row["budget"]) is not int
                or any(not isinstance(row[key], str) for key in expected - {"budget"})):
            raise ValueError("invalid frozen Ghost source")
        sources.append(GhostContextSource(**row))
    return payload["text"], tuple(sources)


def write_provider_artifact(store: ManagedOutputStore, *, session_id: str, run_id: str,
                            effect_id: str, kind: str, payload: object) -> ArtifactRecord:
    if kind not in {REQUEST_KIND, RESPONSE_KIND}:
        raise ValueError("unknown provider artifact kind")
    # A request may be frozen before its external intent is admitted. Its
    # task owns this one body; the later effect references it. Responses are
    # still owned by the already-admitted effect.
    prepared = kind == REQUEST_KIND
    envelope = {"schema_version": 2 if prepared else 1, "kind": kind, "effect_id": effect_id, "payload": payload}
    ref = store.write_json_artifact(session_id=session_id, run_id=run_id, payload=envelope)
    return store.json_artifact_record(ref, owner_task_id=run_id, owner_effect_id=None if prepared else effect_id,
                                      metadata={"kind": kind})


def read_provider_artifact(store: ManagedOutputStore, record: ArtifactRecord, *, kind: str,
                           session_id: str, run_id: str, effect_id: str) -> Any:
    if record.owner_task_id != run_id or record.metadata.get("kind") != kind:
        raise RuntimeEffectError("provider artifact ownership mismatch")
    raw_ref = thaw_json(record.metadata.get("ref"))
    if not isinstance(raw_ref, dict) or raw_ref.get("session_id") != session_id or raw_ref.get("run_id") != run_id:
        raise RuntimeEffectError("provider artifact scope mismatch")
    ref = cast(dict[str, object], raw_ref)
    if ref.get("handle") != record.handle or ref.get("sha256") != record.sha256:
        raise RuntimeEffectError("provider artifact digest reference mismatch")
    payload = store.read_json_artifact(ref)
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "kind", "effect_id", "payload"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] not in ({1, 2} if kind == REQUEST_KIND else {1})
            or payload["kind"] != kind or payload["effect_id"] != effect_id):
        raise RuntimeEffectError("provider artifact envelope mismatch")
    expected_owner = None if kind == REQUEST_KIND and payload["schema_version"] == 2 else effect_id
    if record.owner_effect_id != expected_owner:
        raise RuntimeEffectError("provider artifact ownership mismatch")
    return payload["payload"]


def request_artifact_for_effect(durable: Any, effect: Any) -> ArtifactRecord:
    """Resolve the explicit reference, or decode the version-1 ownership format."""
    if effect.intent.request_ref:
        return cast(ArtifactRecord, durable.artifact(effect.intent.request_ref))
    if effect.intent.schema_version != 1:
        raise RuntimeEffectError("provider intent has no frozen request reference")
    requests = [artifact for artifact in durable.artifacts_for_effect(effect.intent.effect_id)
                if artifact.metadata.get("kind") == REQUEST_KIND]
    if len(requests) != 1:
        raise RuntimeEffectError("legacy provider intent has no unique frozen request")
    return cast(ArtifactRecord, requests[0])


def snapshot_description(snapshot: Any) -> dict[str, object]:
    """Persist the captured contract, never Python callables or a live registry."""
    return {"schema_version": 3, "implementation_id": runtime_implementation_id(), "allowed": json_value(snapshot.allowed),
            "contract_text": snapshot.contract_text, "tool_names": list(snapshot.tool_names),
            "nested_tool_names": list(snapshot.nested_tool_names),
            "research_scope": snapshot.research_scope.to_payload() if snapshot.research_scope is not None else None,
            "policy": snapshot.policy.to_payload(), "specs": json_value(snapshot.frozen_specs),
            "executors": [{"name": name, "binding": None if fn is None else {
                "module": getattr(fn, "__module__", ""), "name": getattr(fn, "__qualname__", ""),
                "version": getattr(fn, "__codey_tool_version__", None),
            }} for name, fn in snapshot.custom_executors]}


@lru_cache(maxsize=1)
def runtime_implementation_id() -> str:
    """Conservative identity for the installed built-in execution code.

    Computed once per process. External executors require their own explicit
    version; their Python names alone do not prove restart compatibility.
    """
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def restore_snapshot(payload: object) -> Any:
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import ToolSpec, custom_executor_for, freeze_spec_parameters

    expected = {"schema_version", "implementation_id", "allowed", "contract_text", "tool_names", "policy", "specs", "executors"}
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] not in (1, 2, 3):
        raise ValueError("invalid captured turn snapshot version/shape")
    extra = {"nested_tool_names", "research_scope"} if payload["schema_version"] == 3 else {"nested_tool_names"} if payload["schema_version"] == 2 else set()
    if set(payload) != expected | extra:
        raise ValueError("invalid captured turn snapshot version/shape")
    names = payload["tool_names"]
    nested = payload.get("nested_tool_names", [])
    allowed = payload["allowed"]
    if (not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names) or not isinstance(payload["contract_text"], str)
            or (allowed is not None and (not isinstance(allowed, list) or any(not isinstance(n, str) for n in allowed)))
            or not isinstance(payload["specs"], list) or not isinstance(payload["executors"], list)
            or not isinstance(nested, list) or any(not isinstance(n, str) or not n for n in nested)
            or len(set(nested)) != len(nested)):
        raise ValueError("invalid captured turn contract")
    specs = []
    spec_fields = {field.name for field in fields(ToolSpec)}
    for raw in payload["specs"]:
        if not isinstance(raw, dict) or set(raw) != spec_fields:
            raise ValueError("invalid captured tool specification")
        row = dict(raw)
        for key in ("aliases", "required", "json_examples"):
            if not isinstance(row[key], list) or any(not isinstance(value, str) for value in row[key]):
                raise ValueError("invalid captured tool sequence")
            row[key] = tuple(row[key])
        row["parameters"] = tuple(tuple(pair) for pair in row["parameters"])
        specs.append(freeze_spec_parameters(ToolSpec(**row)))
    inventory = list(dict.fromkeys([*names, *nested]))
    if [spec.name for spec in specs] != inventory:
        raise ValueError("captured definitions do not match tool names")
    policy = TaskPolicy.from_payload(payload["policy"])
    if nested:
        from codey.codemode.catalog import build_codemode_catalog

        catalog = build_codemode_catalog(specs, allow_writes=policy.allows("project.write"))
        if "codemode" not in names or not set(nested) <= {entry.name for entry in catalog.entries}:
            raise ValueError("captured nested capabilities exceed the frozen CodeMode inventory")
    compatible = payload["implementation_id"] == runtime_implementation_id()
    bindings = []
    for row in payload["executors"]:
        if not isinstance(row, dict) or set(row) != {"name", "binding"} or row["name"] not in inventory:
            raise ValueError("invalid captured executor descriptor")
        name, binding = row["name"], row["binding"]
        current = custom_executor_for(name)
        if binding is None:
            compatible = compatible and current is None
        else:
            if not isinstance(binding, dict) or set(binding) != {"module", "name", "version"}:
                raise ValueError("invalid captured executor identity")
            compatible = compatible and bool(binding["version"]) and current is not None and binding == {
                "module": getattr(current, "__module__", ""), "name": getattr(current, "__qualname__", ""),
                "version": getattr(current, "__codey_tool_version__", None),
            }
        bindings.append((name, current))
    if [name for name, _ in bindings] != inventory:
        raise ValueError("captured executor descriptors do not match tool names")
    from codey.research.answer_contract import AnswerScope

    raw_scope = payload.get("research_scope")
    if isinstance(raw_scope, dict) and "plan_digest" in raw_scope:
        raise ValueError("legacy local Research request is no longer supported; start a new Research task")
    scope = AnswerScope.from_payload(raw_scope) if raw_scope is not None else None
    return TurnSnapshot(allowed=None if allowed is None else tuple(allowed), contract_text=payload["contract_text"],
        tool_names=tuple(names), policy=policy, frozen_specs=tuple(specs),
        custom_executors=tuple(bindings), implementation_compatible=bool(compatible), nested_tool_names=tuple(nested), research_scope=scope)
