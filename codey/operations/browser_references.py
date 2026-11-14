"""Resolve readable control choices against one immutable, receipted observation."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace

from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    parse_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


def _label(value: str) -> str:
    return " ".join(value.split())


@dataclass(frozen=True)
class ObservedControl:
    ref: str
    role: str
    name: str
    context: str
    context_id: str
    disabled: bool

    def payload(self) -> JsonObject:
        return {"ref": self.ref, "role": self.role, "name": self.name, "context": self.context,
                "context_id": self.context_id, "disabled": self.disabled}


@dataclass(frozen=True)
class BrowserReferences:
    receipt_ref: str
    document_json: str
    generation: int
    controls: tuple[ObservedControl, ...]

    def __post_init__(self) -> None:
        if (not self.receipt_ref or self.generation < 0 or not parse_json_object(self.document_json)
                or any(not row.ref or not row.role for row in self.controls)
                or len({row.ref for row in self.controls}) != len(self.controls)):
            raise ValueError("Invalid frozen browser control identity.")

    @classmethod
    def observe(cls, receipt_ref: str, observation: JsonObject, document: JsonObject) -> BrowserReferences:
        if (observation.get("elements_truncated") is not False
                or not (observation.get("complete") is True or observation.get("text_truncated") is True)):
            raise ValueError("Control binding requires complete observed controls and contexts.")
        raw_controls, raw_contexts = observation.get("elements"), observation.get("contexts")
        if not isinstance(raw_controls, list) or not isinstance(raw_contexts, list):
            raise ValueError("Control binding requires observed controls and contexts.")
        contexts: dict[str, str] = {}
        for raw in raw_contexts:
            row = decode_json_object(raw)
            context_id = require_json_string(row.get("id"), "context id")
            if not context_id or context_id in contexts:
                raise ValueError("Invalid observed context identity.")
            contexts[context_id] = require_json_string(row.get("label"), "context label")
        controls: list[ObservedControl] = []
        for raw in raw_controls:
            row = decode_json_object(raw)
            context_id = require_json_string(row.get("context_id"), "control context")
            if context_id and context_id not in contexts:
                raise ValueError("The observed control context is incomplete.")
            controls.append(ObservedControl(
                require_json_string(row.get("ref"), "control ref"), require_json_string(row.get("role"), "control role"),
                require_json_string(row.get("name"), "control name"), contexts.get(context_id, ""), context_id,
                require_json_boolean(row.get("disabled"), "control disabled")))
        return cls(receipt_ref, json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                   require_json_integer(observation.get("generation"), "control generation"), tuple(controls))

    def resolve(self, selector: JsonValue) -> str:
        if not isinstance(selector, dict) or not {"role", "name"} <= set(selector) <= {"role", "name", "context"}:
            raise ValueError("Use a real string ref or {role, name, context?} from the frozen snapshot.")
        role = _label(require_json_string(selector["role"], "role")).lower()
        name = _label(require_json_string(selector["name"], "name"))
        context = _label(require_json_string(selector["context"], "context")) if "context" in selector else None
        matches = [row for row in self.controls if row.role == role and _label(row.name) == name
                   and (context is None or _label(row.context) == context)]
        if len(matches) != 1:
            choices = matches or list(self.controls)
            candidates: list[JsonObject] = [row.payload() for row in choices]
            raise ValueError(("Ambiguous" if matches else "No matching") + " observed control. "
                "Choose by the observed context or use its actual string ref; take a fresh snapshot if needed. Candidates: "
                + json.dumps(candidates, ensure_ascii=False))
        if matches[0].disabled:
            raise ValueError("The selected observed control is disabled: " + json.dumps(matches[0].payload(), ensure_ascii=False))
        return matches[0].ref

    def payload(self) -> JsonObject:
        return {"receipt_ref": self.receipt_ref, "document_json": self.document_json, "generation": self.generation,
                "controls": [row.payload() for row in self.controls]}

    @classmethod
    def restore(cls, raw: JsonValue) -> BrowserReferences:
        row = decode_json_object(raw)
        if set(row) != {"receipt_ref", "document_json", "generation", "controls"} or not isinstance(row["controls"], list):
            raise ValueError("Invalid frozen browser control binding.")
        controls: list[ObservedControl] = []
        for raw_control in row["controls"]:
            control = decode_json_object(raw_control)
            if set(control) != {"ref", "role", "name", "context", "context_id", "disabled"}:
                raise ValueError("Invalid frozen browser control.")
            controls.append(ObservedControl(
                require_json_string(control["ref"], "ref"), require_json_string(control["role"], "role"),
                require_json_string(control["name"], "name"), require_json_string(control["context"], "context"),
                require_json_string(control["context_id"], "context_id"),
                require_json_boolean(control["disabled"], "disabled")))
        return cls(require_json_string(row["receipt_ref"], "receipt_ref"),
                   require_json_string(row["document_json"], "document_json"),
                   require_json_integer(row["generation"], "generation"), tuple(controls))


def reference_spec(spec: ToolSpec, references: BrowserReferences | None) -> ToolSpec:
    """The same ref union serves the outer interface and CodeMode catalog."""
    if references is None or spec.name not in {"browser_fill", "browser_click"}:
        return spec
    selector: JsonObject = {"type": "object", "properties": {
        "role": {"type": "string", "minLength": 1}, "name": {"type": "string"}, "context": {"type": "string"}},
        "required": ["role", "name"], "additionalProperties": False}
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, {"anyOf": [schema, selector]}) if key == "ref" else (key, schema) for key, schema in spec.parameters),
        description=spec.description + " Choose ref as the observed string or {role,name,context?} from the current "
        "frozen snapshot. Context is its observed label. Descriptions must match exactly one control; "
        "for duplicate context labels use the actual string ref."))
