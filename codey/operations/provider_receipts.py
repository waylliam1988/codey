"""Provider request/response artifacts in the existing managed output store.

The body has one owner. Checkpoints and durable artifact rows carry references;
conversation history is produced separately at its generation boundary.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import Field, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

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
    from codey.operations.browser_copy import CopyTurn
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.kernel_protocol import ModelInputBinding, TurnSnapshot
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import ToolSpec

REQUEST_KIND = "provider_request"
RESPONSE_KIND = "provider_response"


def restore_ghost_context(payload: object) -> tuple[str, tuple[GhostContextSource, ...]]:
    payload = decode_json_object(payload)
    if (set(payload) != {"text", "ghost_sources"}
            or not isinstance(payload["text"], str) or not isinstance(payload["ghost_sources"], list)):
        raise ValueError("frozen request has no valid context snapshot")
    sources = []
    source_fields: tuple[Field[object], ...] = fields(GhostContextSource)
    expected = {field.name for field in source_fields}
    for raw in payload["ghost_sources"]:
        row = decode_json_object(raw)
        if (set(row) != expected or type(row["budget"]) is not int
                or any(not isinstance(row[key], str) for key in expected - {"budget"})):
            raise ValueError("invalid frozen Ghost source")
        strings = {key: require_json_string(row[key], key) for key in expected - {"budget"}}
        sources.append(GhostContextSource(budget=require_json_integer(row["budget"], "budget"), **strings))
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
    if not isinstance(raw_ref, dict):
        raise RuntimeEffectError("provider artifact scope mismatch")
    ref: dict[str, object] = dict(raw_ref)
    if ref.get("session_id") != session_id or ref.get("run_id") != run_id:
        raise RuntimeEffectError("provider artifact scope mismatch")
    if ref.get("handle") != record.handle or ref.get("sha256") != record.sha256:
        raise RuntimeEffectError("provider artifact digest reference mismatch")
    raw_payload = store.read_json_artifact(ref)
    if not isinstance(raw_payload, dict):
        raise RuntimeEffectError("provider artifact envelope mismatch")
    payload: dict[object, object] = raw_payload
    if (set(payload) != {"schema_version", "kind", "effect_id", "payload"}
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
    return decode_json_object(json_value({"schema_version": 7,
            "task_navigation_url": snapshot.task_navigation_url,
            "browser_copy": snapshot.browser_copy.payload() if snapshot.browser_copy is not None else None,
            "input_binding": json_value(snapshot.input_binding),
            "implementation_id": runtime_implementation_id(), "allowed": json_value(snapshot.allowed),
            "contract_text": snapshot.contract_text, "tool_names": list(snapshot.tool_names),
            "nested_tool_names": list(snapshot.nested_tool_names),
            "research_scope": snapshot.research_scope.to_payload() if snapshot.research_scope is not None else None,
            "ignore_runtime_claims": snapshot.ignore_runtime_claims,
            "policy": snapshot.policy.to_payload(), "specs": json_value(snapshot.frozen_specs),
            "executors": [{"name": name, "binding": None if fn is None else _executor_identity(fn)}
                          for name, fn in snapshot.custom_executors]}))


def _executor_identity(fn: ToolExecutor) -> dict[str, object]:
    module: object = getattr(fn, "__module__", "")
    name: object = getattr(fn, "__qualname__", "")
    version: object = getattr(fn, "__codey_tool_version__", None)
    return {"module": module, "name": name, "version": version}


class _CachedIdentity(Protocol):
    def __call__(self) -> str: ...

    def cache_clear(self) -> None: ...


_cache_identity: Callable[[Callable[[], str]], _CachedIdentity] = lru_cache(maxsize=1)


@_cache_identity
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


def _restore_spec(value: object) -> ToolSpec:
    from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters

    row = decode_json_object(value)
    spec_fields: tuple[Field[object], ...] = fields(ToolSpec)
    expected = {field.name for field in spec_fields}
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


def _restore_input_binding(payload: object, names: list[str], specs: list[ToolSpec],
                           copy: CopyTurn | None, *, nested: list[str], policy: TaskPolicy,
                           navigation_url: str | None) -> ModelInputBinding | None:
    from codey.operations.kernel_protocol import ModelInputBinding
    from codey.policies.tool_requirement import has_structured_actions

    if payload is None:
        if (copy is not None and "codemode" in names) or has_structured_actions(policy.required_checks):
            raise ValueError("captured structured input has no input binding")
        return None
    raw = decode_json_object(payload)
    if set(raw) != {"kind", "model_spec"}:
        raise ValueError("invalid captured input binding")
    model = _restore_spec(raw["model_spec"])
    if raw["kind"] == "upload":
        binding = ModelInputBinding("upload", model)
    elif raw["kind"] == "browser_copy":
        binding = ModelInputBinding("browser_copy", model)
    elif raw["kind"] == "actions":
        binding = ModelInputBinding("actions", model)
    else:
        raise ValueError("unknown captured input binding")
    execution = next((spec for spec in specs if spec.name == model.name), None)
    if execution is None or model.name != "codemode" or model.name not in names:
        raise ValueError("captured input binding has no visible executor")
    if replace(model, parameters=execution.parameters, required=execution.required, description=execution.description,
               declarations=execution.declarations, json_examples=execution.json_examples) != execution:
        raise ValueError("captured input binding changes execution authority")
    if binding.kind == "actions":
        from codey.operations.structured_actions import action_model_spec

        leaves = tuple(spec for spec in specs if spec.name in nested)
        if (copy is not None or policy.strict_research or not has_structured_actions(policy.required_checks)
                or any(not policy.allows(leaf.grant) for leaf in leaves)
                or action_model_spec(execution, leaves, navigation_url) != model):
            raise ValueError("captured action input differs from its frozen permissions")
    elif has_structured_actions(policy.required_checks):
        raise ValueError("captured action requirement has an incompatible input binding")
    elif binding.kind == "browser_copy":
        if copy is None or copy.phase not in {"observe", "edit"} or copy.spec(execution) != model:
            raise ValueError("captured input binding differs from its copy observation")
    elif copy is not None or set(dict(model.parameters)) != {"upload"} or model.required != ("upload",):
        raise ValueError("invalid captured upload input binding")
    return binding


def restore_snapshot(payload: object) -> TurnSnapshot:
    from codey.operations.kernel_protocol import TurnSnapshot, render_snapshot_contract
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import custom_executor_for

    payload = decode_json_object(payload)
    expected = {"schema_version", "implementation_id", "allowed", "contract_text", "tool_names", "policy", "specs", "executors",
                "nested_tool_names", "research_scope", "ignore_runtime_claims", "browser_copy", "input_binding"}
    version = require_json_integer(payload.get("schema_version"), "snapshot version")
    if version not in {6, 7}:
        raise ValueError("unsupported captured turn snapshot version; start a new request")
    if version == 7:
        expected.add("task_navigation_url")
    if set(payload) != expected:
        raise ValueError("invalid captured turn snapshot version/shape")
    names = require_json_strings(payload["tool_names"], "tool_names")
    nested = require_json_strings(payload["nested_tool_names"], "nested_tool_names")
    allowed = None if payload["allowed"] is None else require_json_strings(payload["allowed"], "allowed")
    contract = require_json_string(payload["contract_text"], "contract_text")
    if any(not name for name in (*names, *nested)) or len(set(names)) != len(names) or len(set(nested)) != len(nested):
        raise ValueError("invalid captured turn contract")
    raw_specs, raw_executors = payload["specs"], payload["executors"]
    if not isinstance(raw_specs, list) or not isinstance(raw_executors, list):
        raise ValueError("invalid captured turn definitions")
    specs = [_restore_spec(raw) for raw in raw_specs]
    inventory_names: dict[str, None] = dict.fromkeys([*names, *nested])
    inventory = list(inventory_names)
    if [spec.name for spec in specs] != inventory:
        raise ValueError("captured definitions do not match tool names")
    policy = TaskPolicy.from_payload(payload["policy"])
    navigation_url = payload.get("task_navigation_url")
    if navigation_url is not None:
        from codey.policies.network import check_fetch_url

        navigation_url = require_json_string(navigation_url, "task_navigation_url")
        navigation = next((spec for spec in specs if spec.name == "browser_navigate"), None)
        raw_binding = payload["input_binding"]
        if (not navigation_url or check_fetch_url(navigation_url, resolve=False) is not None
                or "codemode" not in names or "browser_navigate" not in nested
                or navigation is None or not policy.allows(navigation.grant) or policy.strict_research
                or (raw_binding is not None and (not isinstance(raw_binding, dict) or raw_binding.get("kind") != "actions"))
                or payload["browser_copy"] is not None):
            raise ValueError("invalid captured task navigation binding")
    ignore_runtime_claims = require_json_boolean(payload["ignore_runtime_claims"], "ignore_runtime_claims")
    if nested:
        from codey.codemode.catalog import build_codemode_catalog

        catalog = build_codemode_catalog(
            specs, allow_writes=policy.allows("project.write"),
            allow_project_edit="edit" in nested,
            allow_project_run="run" in nested,
            task_navigation_url=navigation_url,
        )
        if "codemode" not in names or not set(nested) <= {entry.name for entry in catalog.entries}:
            raise ValueError("captured nested capabilities exceed the frozen CodeMode inventory")
    compatible = payload["implementation_id"] == runtime_implementation_id()
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
            compatible = (compatible and bool(binding["version"]) and current is not None
                          and binding == _executor_identity(current))
        bindings.append((name, current))
    if [name for name, _ in bindings] != inventory:
        raise ValueError("captured executor descriptors do not match tool names")
    from codey.research.answer_contract import AnswerScope

    raw_scope = payload["research_scope"]
    scope = AnswerScope.from_payload(raw_scope) if raw_scope is not None else None
    from codey.operations.browser_copy import CopyTurn

    copy = CopyTurn.restore(decode_json_object(payload["browser_copy"])) if payload["browser_copy"] is not None else None
    input_binding = _restore_input_binding(payload["input_binding"], names, specs, copy,
                                          nested=nested, policy=policy, navigation_url=navigation_url)
    snapshot = TurnSnapshot(allowed=None if allowed is None else tuple(allowed), contract_text=contract,
        tool_names=tuple(names), policy=policy, frozen_specs=tuple(specs),
        custom_executors=tuple(bindings), implementation_compatible=bool(compatible), nested_tool_names=tuple(nested),
        research_scope=scope, ignore_runtime_claims=ignore_runtime_claims,
        browser_copy=copy, input_binding=input_binding, task_navigation_url=navigation_url)
    from codey.toolchain.tool_prompt import contract_semantics

    if contract_semantics(contract) != contract_semantics(render_snapshot_contract(snapshot)):
        raise ValueError("captured contract text differs from its frozen model definitions")
    return snapshot
