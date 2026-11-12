"""Provider request/response artifacts in the existing managed output store.

The body has one owner. Checkpoints and durable artifact rows carry references;
conversation history is produced separately at its generation boundary.
"""
from __future__ import annotations

import hashlib
from dataclasses import fields
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from codey.durable.model import ArtifactRecord, thaw_json
from codey.durable.session import DurableSession
from codey.providers.response_record import json_value
from codey.runtime.effects.effect_records import RuntimeEffectError, RuntimeEffectProjection
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
    require_json_strings,
)
from codey.workspace.context_admission import GhostContextSource

if TYPE_CHECKING:
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.toolchain.tool_spec import ToolSpec

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
                           session_id: str, run_id: str, effect_id: str) -> JsonObject:
    if record.owner_task_id != run_id or record.metadata.get("kind") != kind:
        raise RuntimeEffectError("provider artifact ownership mismatch")
    raw_ref = thaw_json(record.metadata.get("ref"))
    if not isinstance(raw_ref, dict) or raw_ref.get("session_id") != session_id or raw_ref.get("run_id") != run_id:
        raise RuntimeEffectError("provider artifact scope mismatch")
    ref: dict[str, object] = dict(raw_ref)
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
    return decode_json_object(payload["payload"])


def request_artifact_for_effect(durable: DurableSession, effect: RuntimeEffectProjection) -> ArtifactRecord:
    """Resolve the explicit reference, or decode the version-1 ownership format."""
    if effect.intent.request_ref:
        return durable.artifact(effect.intent.request_ref)
    if effect.intent.schema_version != 1:
        raise RuntimeEffectError("provider intent has no frozen request reference")
    requests = [artifact for artifact in durable.artifacts_for_effect(effect.intent.effect_id)
                if artifact.metadata.get("kind") == REQUEST_KIND]
    if len(requests) != 1:
        raise RuntimeEffectError("legacy provider intent has no unique frozen request")
    return requests[0]


def snapshot_description(snapshot: TurnSnapshot) -> JsonObject:
    """Persist the captured contract, never Python callables or a live registry."""
    if snapshot.policy is None:
        raise ValueError("captured turn has no authorization policy")
    return decode_json_object(json_value({"schema_version": 5 if snapshot.browser_copy is not None else 4,
            **({"browser_copy": snapshot.browser_copy.payload()} if snapshot.browser_copy is not None else {}),
            "implementation_id": runtime_implementation_id(), "allowed": json_value(snapshot.allowed),
            "contract_text": snapshot.contract_text, "tool_names": list(snapshot.tool_names),
            "nested_tool_names": list(snapshot.nested_tool_names),
            "research_scope": snapshot.research_scope.to_payload() if snapshot.research_scope is not None else None,
            "ignore_runtime_claims": snapshot.ignore_runtime_claims,
            "policy": snapshot.policy.to_payload(), "specs": json_value(snapshot.frozen_specs),
            "executors": [{"name": name, "binding": None if fn is None else {
                "module": getattr(fn, "__module__", ""), "name": getattr(fn, "__qualname__", ""),
                "version": getattr(fn, "__codey_tool_version__", None),
            }} for name, fn in snapshot.custom_executors]}))


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


def _restore_spec(value: object, *, snapshot_version: int) -> ToolSpec:
    from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters

    row = decode_json_object(value)
    expected = {field.name for field in fields(ToolSpec)}
    legacy_metadata: JsonObject = {"namespace": "core", "effect": "read", "evidence_route": "none", "preconditions": []}
    if snapshot_version <= 3 and set(row) == expected - legacy_metadata.keys():
        # Original snapshots predate catalog metadata. Decode for inspection;
        # restore_snapshot keeps these contracts incompatible with execution.
        row.update(legacy_metadata)
    if set(row) != expected:
        raise ValueError("invalid captured tool specification")
    parameters = row["parameters"]
    if not isinstance(parameters, list):
        raise ValueError("invalid captured tool parameters")
    pairs: list[tuple[str, object]] = []
    for pair in parameters:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid captured tool parameter pair")
        pairs.append((require_json_string(pair[0], "parameter name"), pair[1]))
    return freeze_spec_parameters(ToolSpec(
        name=require_json_string(row["name"], "tool name"),
        aliases=tuple(require_json_strings(row["aliases"], "aliases")),
        grant=require_json_string(row["grant"], "grant"), parameters=tuple(pairs),
        required=tuple(require_json_strings(row["required"], "required")),
        json_examples=tuple(require_json_strings(row["json_examples"], "json_examples")),
        description=require_json_string(row["description"], "description"),
        executor=require_json_string(row["executor"], "executor"),
        replay_class=require_json_string(row["replay_class"], "replay_class"),
        parallel_safe=require_json_boolean(row["parallel_safe"], "parallel_safe"),
        output_schema=decode_json_object(row["output_schema"]),
        declarations=require_json_string(row["declarations"], "declarations"),
        namespace=require_json_string(row["namespace"], "namespace"),
        effect=require_json_string(row["effect"], "effect"),
        evidence_route=require_json_string(row["evidence_route"], "evidence_route"),
        preconditions=tuple(require_json_strings(row["preconditions"], "preconditions"))))


def restore_snapshot(payload: object) -> TurnSnapshot:
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import custom_executor_for

    payload = decode_json_object(payload)
    expected = {"schema_version", "implementation_id", "allowed", "contract_text", "tool_names", "policy", "specs", "executors"}
    version = require_json_integer(payload.get("schema_version"), "snapshot version")
    if version not in (1, 2, 3, 4, 5):
        raise ValueError("invalid captured turn snapshot version/shape")
    extra = {"nested_tool_names", "research_scope"} if version >= 3 else {"nested_tool_names"} if version == 2 else set()
    if version >= 4:
        extra.add("ignore_runtime_claims")
    if version == 5:
        extra.add("browser_copy")
    if set(payload) != expected | extra:
        raise ValueError("invalid captured turn snapshot version/shape")
    names = require_json_strings(payload["tool_names"], "tool_names")
    nested = require_json_strings(payload.get("nested_tool_names", []), "nested_tool_names")
    allowed = None if payload["allowed"] is None else require_json_strings(payload["allowed"], "allowed")
    contract = require_json_string(payload["contract_text"], "contract_text")
    if any(not name for name in (*names, *nested)) or len(set(names)) != len(names) or len(set(nested)) != len(nested):
        raise ValueError("invalid captured turn contract")
    raw_specs, raw_executors = payload["specs"], payload["executors"]
    if not isinstance(raw_specs, list) or not isinstance(raw_executors, list):
        raise ValueError("invalid captured turn definitions")
    legacy_specs = any(isinstance(raw, dict) and "namespace" not in raw for raw in raw_specs)
    specs = [_restore_spec(raw, snapshot_version=version) for raw in raw_specs]
    inventory = list(dict.fromkeys([*names, *nested]))
    if [spec.name for spec in specs] != inventory:
        raise ValueError("captured definitions do not match tool names")
    policy = TaskPolicy.from_payload(payload["policy"])
    ignore_runtime_claims = require_json_boolean(payload["ignore_runtime_claims"], "ignore_runtime_claims") if version >= 4 else False
    if nested:
        from codey.codemode.catalog import build_codemode_catalog

        catalog = build_codemode_catalog(
            specs, allow_writes=policy.allows("project.write"),
            allow_project_edit="edit" in nested,
            allow_project_run="run" in nested,
        )
        if "codemode" not in names or not set(nested) <= {entry.name for entry in catalog.entries}:
            raise ValueError("captured nested capabilities exceed the frozen CodeMode inventory")
    compatible = not legacy_specs and payload["implementation_id"] == runtime_implementation_id()
    bindings: list[tuple[str, ToolExecutor | None]] = []
    for raw in raw_executors:
        row = decode_json_object(raw)
        name = require_json_string(row.get("name"), "executor name")
        if set(row) != {"name", "binding"} or name not in inventory:
            raise ValueError("invalid captured executor descriptor")
        binding = row["binding"]
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
    from codey.operations.browser_copy import CopyTurn

    return TurnSnapshot(allowed=None if allowed is None else tuple(allowed), contract_text=contract,
        tool_names=tuple(names), policy=policy, frozen_specs=tuple(specs),
        custom_executors=tuple(bindings), implementation_compatible=bool(compatible), nested_tool_names=tuple(nested),
        research_scope=scope, ignore_runtime_claims=ignore_runtime_claims,
        browser_copy=CopyTurn.restore(decode_json_object(payload["browser_copy"])) if version == 5 else None)
