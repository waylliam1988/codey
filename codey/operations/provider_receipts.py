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
from codey.workspace.context_admission import ContextCandidate

if TYPE_CHECKING:
    from codey.operations.browser_copy import CopyTurn
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.kernel_protocol import InputContractVersion, ModelInputBinding, TurnSnapshot
    from codey.operations.project_instructions import ProjectInstructionBinding
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import ToolSpec

REQUEST_KIND = "provider_request"
RESPONSE_KIND = "provider_response"


def restore_context(payload: object) -> tuple[str, tuple[ContextCandidate, ...]]:
    payload = decode_json_object(payload)
    if (set(payload) != {"text", "memory_sources"}
            or not isinstance(payload["text"], str) or not isinstance(payload["memory_sources"], list)):
        raise ValueError("frozen request has no valid context snapshot")
    sources = []
    source_fields: tuple[Field[object], ...] = fields(ContextCandidate)
    expected = {field.name for field in source_fields}
    for raw in payload["memory_sources"]:
        row = decode_json_object(raw)
        if (set(row) != expected or type(row["budget"]) is not int
                or any(not isinstance(row[key], str) for key in expected - {"budget", "source_refs"})):
            raise ValueError("invalid frozen context source")
        sources.append(ContextCandidate(
            key=require_json_string(row["key"], "key"),
            text=require_json_string(row["text"], "text"),
            budget=require_json_integer(row["budget"], "budget"),
            scope=require_json_string(row["scope"], "scope"),
            capability_id=require_json_string(row["capability_id"], "capability_id"),
            admission_reason=require_json_string(row["admission_reason"], "admission_reason"),
            expires_at=require_json_string(row["expires_at"], "expires_at"),
            source_refs=tuple(require_json_strings(row["source_refs"], "source_refs")),
        ))
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
    if not effect.intent.request_ref:
        raise RuntimeEffectError("provider intent has no frozen request reference")
    return durable.artifact(effect.intent.request_ref)


def snapshot_description(snapshot: TurnSnapshot) -> JsonObject:
    """Persist the captured contract, never Python callables or a live registry."""
    if snapshot.policy is None:
        raise ValueError("captured turn has no authorization policy")
    payload = decode_json_object(json_value({"schema_version": snapshot.input_contract_version,
            "task_navigation_url": snapshot.task_navigation_url,
            "browser_copy": snapshot.browser_copy.payload() if snapshot.browser_copy is not None else None,
            "input_binding": json_value(snapshot.input_binding),
            "implementation_id": runtime_implementation_id() if snapshot.implementation_compatible else "",
            "allowed": json_value(snapshot.allowed),
            "contract_text": snapshot.contract_text, "tool_names": list(snapshot.tool_names),
            "nested_tool_names": list(snapshot.nested_tool_names),
            "research_scope": snapshot.research_scope.to_payload() if snapshot.research_scope is not None else None,
            "ignore_runtime_claims": snapshot.ignore_runtime_claims,
            "policy": snapshot.policy.to_payload(), "specs": json_value(snapshot.frozen_specs),
            "executors": [{"name": name, "binding": None if fn is None else _executor_identity(fn)}
                          for name, fn in snapshot.custom_executors]}))
    if snapshot.input_contract_version >= 15:
        payload["task_source_url"] = snapshot.task_source_url
    if snapshot.input_contract_version >= 18:
        payload["project_instructions"] = [rule.to_payload() for rule in snapshot.project_instructions]
    if snapshot.input_contract_version >= 19:
        payload["navigation_verifies"] = snapshot.navigation_verifies
    if snapshot.input_contract_version >= 20:
        payload["data_file_reads"] = list(snapshot.data_file_reads)
    if snapshot.input_contract_version >= 22:
        payload["symbol_read_refs"] = dict(snapshot.symbol_read_refs)
    payload["browser_fill_text"] = snapshot.browser_fill_text
    payload["browser_followups"] = snapshot.browser_followups
    payload["browser_references"] = snapshot.browser_references.payload() if snapshot.browser_references else None
    return payload


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
        supports_background=require_json_boolean(row["supports_background"], "supports_background"),
        output_schema=decode_json_object(row["output_schema"]),
        declarations=require_json_string(row["declarations"], "declarations"),
        namespace=require_json_string(row["namespace"], "namespace"),
        effect=require_json_string(row["effect"], "effect"),
        evidence_route=require_json_string(row["evidence_route"], "evidence_route"),
        preconditions=tuple(require_json_strings(row["preconditions"], "preconditions"))))


def _restore_input_binding(payload: object, names: list[str], specs: list[ToolSpec],
                           copy: CopyTurn | None, policy: TaskPolicy) -> tuple[ModelInputBinding | None, bool]:
    from codey.operations.kernel_protocol import ModelInputBinding

    if payload is None:
        if copy is not None and "codemode" in names:
            raise ValueError("captured structured input has no input binding")
        return None, True
    raw = decode_json_object(payload)
    if set(raw) not in ({"kind", "model_spec"}, {"kind", "model_spec", "source_refs"}):
        raise ValueError("invalid captured input binding")
    source_refs = tuple(require_json_strings(raw.get("source_refs", []), "bound source receipts"))
    if source_refs and raw["kind"] != "declared_fields":
        raise ValueError("source receipts require a declared-field binding")
    if raw["kind"] == "actions":
        raise ValueError("captured codemode.action input is no longer supported; start a new request")
    model = _restore_spec(raw["model_spec"])
    if raw["kind"] == "upload":
        binding = ModelInputBinding("upload", model)
    elif raw["kind"] == "browser_copy":
        binding = ModelInputBinding("browser_copy", model)
    elif raw["kind"] == "declared_fields":
        binding = ModelInputBinding("declared_fields", model, source_refs)
    else:
        raise ValueError("unknown captured input binding")
    execution = next((spec for spec in specs if spec.name == model.name), None)
    if (execution is None or model.name not in names
            or model.name != "codemode" and not (binding.kind == "declared_fields" and model.name == "edit")
            or binding.kind == "declared_fields" and "codemode" not in names):
        raise ValueError("captured input binding has no visible executor")
    if replace(model, parameters=execution.parameters, required=execution.required, description=execution.description,
               declarations=execution.declarations, json_examples=execution.json_examples) != execution:
        raise ValueError("captured input binding changes execution authority")
    if binding.kind == "browser_copy":
        if copy is None or copy.phase not in {"observe", "edit"}:
            raise ValueError("captured input binding has no copy observation")
        # Historical replies keep their frozen contract. Only execution needs
        # the current lowering implementation to match that contract.
        return binding, copy.spec(execution) == model
    elif binding.kind == "declared_fields":
        from codey.operations.declared_field_input import model_spec, write_form
        from codey.operations.file_field_binding import file_rules
        from codey.toolchain.tool_spec import thaw_schema_value

        parameters = dict(model.parameters)
        path_schema = decode_json_object(thaw_schema_value(parameters.get("path")))
        paths = tuple(require_json_strings(path_schema.get("enum"), "declared destination paths"))
        edit = next((spec for spec in specs if spec.name == "edit"), None)
        if (copy is not None or not paths or not set(paths) <= {rule.file_path for rule in file_rules(policy.required_checks)}
                or not {"read_file", "edit"} <= {spec.name for spec in specs} or edit is None
                or write_form(edit) is None or model_spec(execution, paths) != model
                or write_form(edit) == "content" and not source_refs):
            raise ValueError("invalid captured declared-field input binding")
    elif copy is not None or set(dict(model.parameters)) != {"upload"} or model.required != ("upload",):
        raise ValueError("invalid captured upload input binding")
    return binding, True


def _snapshot_payload(raw: object) -> tuple[JsonObject, InputContractVersion]:
    payload = decode_json_object(raw)
    expected = {"schema_version", "implementation_id", "allowed", "contract_text", "tool_names", "policy", "specs", "executors",
                "nested_tool_names", "research_scope", "ignore_runtime_claims", "browser_copy", "input_binding",
                "task_navigation_url", "browser_fill_text", "browser_followups", "browser_references"}
    version = require_json_integer(payload.get("schema_version"), "snapshot version")
    if version >= 15:
        expected.add("task_source_url")
    if version >= 18:
        expected.add("project_instructions")
    if version >= 19:
        expected.add("navigation_verifies")
    if version >= 20:
        expected.add("data_file_reads")
    if version >= 22:
        expected.add("symbol_read_refs")
    match version:
        case 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22:
            if set(payload) != expected:
                raise ValueError("invalid captured turn snapshot version/shape")
            return payload, version
        case _:
            raise ValueError("unsupported captured turn snapshot version; start a new request")


def _restore_model_contract(snapshot: TurnSnapshot, raw_references: object) -> TurnSnapshot:
    from codey.operations.kernel_protocol import render_snapshot_contract
    from codey.toolchain.tool_prompt import contract_semantics

    contract = contract_semantics(snapshot.contract_text)
    references = snapshot.browser_references
    if (contract != contract_semantics(render_snapshot_contract(snapshot))
            and references is not None and isinstance(raw_references, dict)
            and "presentation_version" not in raw_references):
        # Two historical presentations shared snapshot version 9. Select only
        # a presentation whose complete generated contract matches the artifact.
        snapshot = replace(snapshot, browser_references=replace(references, presentation_version=1))
    if contract != contract_semantics(render_snapshot_contract(snapshot)):
        raise ValueError("captured contract text differs from its frozen model definitions")
    return snapshot


def _restore_bound_source(
    payload: JsonObject,
    navigation_url: str | None,
    names: list[str],
    nested: list[str],
    policy: TaskPolicy,
) -> str | None:
    value = payload.get("task_source_url")
    if value is None:
        return None
    from codey.policies.network import check_fetch_url

    source = require_json_string(value, "task_source_url")
    if (not source or navigation_url is None or (source == navigation_url and payload["schema_version"] == 15)
            or check_fetch_url(source, resolve=False) is not None
            or "open_url" not in (*names, *nested) or not policy.allows("web.read")):
        raise ValueError("invalid captured task source binding")
    return source


def _restore_project_instructions(payload: JsonObject, policy: TaskPolicy) -> tuple[ProjectInstructionBinding, ...]:
    from codey.operations.project_instructions import ProjectInstructionBinding

    raw = payload.get("project_instructions", [])
    if not isinstance(raw, list):
        raise ValueError("invalid captured project instruction collection")
    instructions = tuple(ProjectInstructionBinding.from_payload(rule) for rule in raw)
    if len({rule.name for rule in instructions}) != len(instructions) or instructions and not policy.allows("project.write"):
        raise ValueError("invalid captured project instruction scope")
    return instructions


def _restore_navigation_verification(payload: JsonObject, navigation_url: str | None, followups: bool,
                                     names: list[str], nested: list[str]) -> bool:
    verifies = require_json_boolean(payload.get("navigation_verifies", False), "navigation_verifies")
    if verifies and (not followups or navigation_url is None
                     or "browser_navigate" not in names or not {"browser_snapshot", "browser_read"} <= set(nested)):
        raise ValueError("invalid captured application navigation verification")
    return verifies


def restore_snapshot(payload: object) -> TurnSnapshot:
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.policies.output_requirement import explanation_spec, has_explanation
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import custom_executor_for

    payload, input_version = _snapshot_payload(payload)
    raw_binding = payload["input_binding"]
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
    from codey.memory.candidates import CANDIDATE_FIELD
    from codey.operations.memory_candidate_transport import memory_candidate_spec

    if has_explanation(policy.required_checks) and any(
            spec.name == "done" and spec != (
                memory_candidate_spec(explanation_spec(spec)) if CANDIDATE_FIELD in dict(spec.parameters)
                else explanation_spec(spec)) for spec in specs):
        raise ValueError("unsupported captured explanation contract; start a new request")
    from codey.operations.browser_copy import CopyTurn

    copy = CopyTurn.restore(decode_json_object(payload["browser_copy"])) if payload["browser_copy"] is not None else None
    input_binding, binding_compatible = _restore_input_binding(raw_binding, names, specs, copy, policy)
    navigation_url = payload.get("task_navigation_url")
    if navigation_url is not None:
        from codey.policies.network import check_fetch_url

        navigation_url = require_json_string(navigation_url, "task_navigation_url")
        targets = {"browser_navigate", "open_url"} if input_version >= 13 else {"browser_navigate"}
        navigation = next((spec for spec in specs if spec.name in targets and spec.name in (*names, *nested)), None)
        if (not navigation_url or check_fetch_url(navigation_url, resolve=False) is not None
                or "codemode" not in names and (input_version < 13 or "open_url" not in names)
                or navigation is None or not policy.allows(navigation.grant)
                or input_binding is not None and input_binding.kind != "declared_fields"
                or copy is not None):
            raise ValueError("invalid captured task navigation binding")
    source_url = _restore_bound_source(payload, navigation_url, names, nested, policy)
    ignore_runtime_claims = require_json_boolean(payload["ignore_runtime_claims"], "ignore_runtime_claims")
    if nested:
        from codey.codemode.catalog import build_codemode_catalog

        catalog = build_codemode_catalog(
            specs, allow_writes=policy.allows("project.write"),
            allow_project_edit="edit" in nested,
            allow_project_run="run" in nested,
            task_navigation_url=navigation_url,
            task_source_url=source_url,
            task_source_selector=input_version >= 13 and (source_url is not None or input_version < 15),
        )
        if "codemode" not in names or not set(nested) <= {entry.name for entry in catalog.entries}:
            raise ValueError("captured nested capabilities exceed the frozen CodeMode inventory")
    compatible = payload["implementation_id"] == runtime_implementation_id() and binding_compatible
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

    scope = AnswerScope.from_payload(payload["research_scope"]) if payload["research_scope"] is not None else None
    from codey.operations.browser_followups import FOLLOWUP_TOOLS, MAX_TEXT, allows_surface

    fill_text = payload.get("browser_fill_text")
    if fill_text is not None and (not isinstance(fill_text, str) or not fill_text.strip() or len(fill_text) > MAX_TEXT):
        raise ValueError("invalid captured browser fill text")
    from codey.operations.browser_references import BrowserReferences

    followups = require_json_boolean(payload["browser_followups"], "browser_followups")
    raw_references = payload.get("browser_references")
    references = BrowserReferences.restore(raw_references) if raw_references is not None else None
    if (fill_text is not None and not followups or references is not None
            and (input_binding is not None and input_binding.kind != "declared_fields"
                 or copy is not None or not any(spec.executor == "browser" and "ref" in dict(spec.parameters)
                     and spec.name in nested for spec in specs))):
        raise ValueError("invalid captured browser input binding")
    snapshot = TurnSnapshot(allowed=None if allowed is None else tuple(allowed), contract_text=contract,
        tool_names=tuple(names), policy=policy, frozen_specs=tuple(specs),
        custom_executors=tuple(bindings), implementation_compatible=bool(compatible), nested_tool_names=tuple(nested),
        research_scope=scope, ignore_runtime_claims=ignore_runtime_claims,
        browser_copy=copy, input_binding=input_binding, task_navigation_url=navigation_url,
        input_contract_version=input_version, task_source_url=source_url,
        browser_followups=followups, browser_fill_text=fill_text, browser_references=references,
        project_instructions=_restore_project_instructions(payload, policy),
        navigation_verifies=_restore_navigation_verification(payload, navigation_url, followups, names, nested),
        data_file_reads=tuple(require_json_strings(payload.get("data_file_reads", []), "data file reads")),
        symbol_read_refs=tuple(sorted((path, require_json_string(ref, "symbol read receipt"))
                               for path, ref in decode_json_object(payload.get("symbol_read_refs", {})).items())))
    from codey.toolchain.constants import PAGE_DETAIL_TOOL

    # Native disclosure reads have their own frozen choice contract. They
    # coexist with, but do not lower through, the browser follow-up programs.
    direct = tuple(spec.name for spec in specs if spec.executor == "browser" and spec.name in names and spec.name != PAGE_DETAIL_TOOL)
    research = snapshot.research_scope is not None
    from codey.toolchain.browser_native_specs import browser_native_specs

    direct_surface = set(FOLLOWUP_TOOLS) | set(browser_native_specs())
    if snapshot.browser_followups and (not set(direct) <= direct_surface
            or not allows_surface(snapshot, direct, research=research)):
        raise ValueError("captured browser follow-ups exceed the authorized browser interface")
    return _restore_model_contract(snapshot, raw_references)
