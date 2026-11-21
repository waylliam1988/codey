"""Resolve readable control choices against one immutable, receipted observation."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from codey.runtime.core.models import ToolResult
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

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserToolOutput


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
    presentation_version: Literal[1, 2, 3] = 3

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

    def _matches(self, selector: JsonValue) -> list[ObservedControl]:
        if not isinstance(selector, dict) or not {"role", "name"} <= set(selector) <= {"role", "name", "context"}:
            raise ValueError("Use a real string ref or {role, name, context?} from the frozen snapshot.")
        role = _label(require_json_string(selector["role"], "role")).lower()
        name = _label(require_json_string(selector["name"], "name"))
        context = _label(require_json_string(selector["context"], "context")) if "context" in selector else None
        return [row for row in self.controls if row.role == role and _label(row.name) == name
                and (context is None or _label(row.context) == context)]

    def choices(self, action: str = "") -> list[JsonValue]:
        """Offer complete, uniquely resolvable combinations from this observation."""
        choices: list[JsonValue] = []
        for row in self.controls:
            if row.disabled:
                continue
            if self.presentation_version >= 3:
                if action == "browser_fill" and row.role not in {"textbox", "combobox", "searchbox"}:
                    continue
                if action == "browser_click" and row.role in {"textbox", "searchbox", "file_input"}:
                    continue
            base: JsonObject = {"role": row.role, "name": row.name}
            for selector in (base, {**base, "context": row.context}):
                if len(self._matches(selector)) == 1 and selector not in choices:
                    choices.append(selector)
                    if self.presentation_version >= 3:
                        break
        return choices

    def resolve(self, selector: JsonValue) -> str:
        matches = self._matches(selector)
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
                "controls": [row.payload() for row in self.controls], "presentation_version": self.presentation_version}

    @classmethod
    def restore(cls, raw: JsonValue) -> BrowserReferences:
        row = decode_json_object(raw)
        expected = {"receipt_ref", "document_json", "generation", "controls"}
        if set(row) not in (expected, expected | {"presentation_version"}) or not isinstance(row["controls"], list):
            raise ValueError("Invalid frozen browser control binding.")
        raw_version = require_json_integer(row.get("presentation_version", 2), "control presentation version")
        presentation_version: Literal[1, 2, 3]
        if raw_version == 1:
            presentation_version = 1
        elif raw_version == 2:
            presentation_version = 2
        elif raw_version == 3:
            presentation_version = 3
        else:
            raise ValueError("Unsupported frozen browser control presentation version.")
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
                   require_json_integer(row["generation"], "generation"), tuple(controls), presentation_version)


def reference_spec(spec: ToolSpec, references: BrowserReferences | None) -> ToolSpec:
    """The same ref union serves the outer interface and CodeMode catalog."""
    if references is None or spec.name not in {"browser_fill", "browser_click"}:
        return spec
    choices = references.choices(spec.name)
    selector: JsonObject = {"type": "object", "properties": {
        "role": {"type": "string", "minLength": 1}, "name": {"type": "string"}, "context": {"type": "string"}},
        "required": ["role", "name"], "additionalProperties": False}
    if references.presentation_version == 1:
        # Decode the original presentation through the same frozen identity and
        # execution contract. This does not install an older executor.
        return freeze_spec_parameters(replace(spec, parameters=tuple(
            (key, {"anyOf": [schema, selector]}) if key == "ref" else (key, schema) for key, schema in spec.parameters),
            description=spec.description + " Choose ref as the observed string or {role,name,context?} from the current "
            "frozen snapshot. Context is its observed label. Descriptions must match exactly one control; "
            "for duplicate context labels use the actual string ref."))
    selector["enum"] = choices
    example = " Example ref argument: " + json.dumps(choices[0], ensure_ascii=False) if choices else ""
    if references.presentation_version >= 3:
        return freeze_spec_parameters(replace(spec, parameters=tuple(
            (key, {"anyOf": [selector, schema]} if choices else schema) if key == "ref" else (key, schema)
            for key, schema in spec.parameters), json_examples=(),
            description=spec.description + " Select a complete observed role/name/context choice. "
            "A raw ref is available for controls observed inside CodeMode. "
            "Copy only ref, never a display row. Reobserve when the target is absent." + example))
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, {"anyOf": [selector, schema]} if choices else schema) if key == "ref" else (key, schema)
        for key, schema in spec.parameters),
        json_examples=(),
        description=spec.description + " Prefer a complete structured ref choice from the current frozen observation. "
        "Each offered combination selects exactly one enabled control. A string ref remains available for "
        "observed controls, including fresh refs acquired within a program or duplicate context labels. "
        "Copy only the ref field, never a whole display row. If the target is absent or controls are truncated, "
        "observe again or expand browser_snapshot before choosing; execution still verifies the original node." + example))


def reference_failure(output: BrowserToolOutput, results: Mapping[str, ToolResult]) -> BrowserToolOutput:
    """Diagnose a missing live ref from original receipts, without rebinding it."""
    value = output.observation
    supplied = value.get("ref")
    if (output.ok or value.get("reason") != "unknown_ref" or value.get("delivery") != "not_delivered"
            or not isinstance(supplied, str)):
        return output
    for receipt_ref, receipt in reversed(tuple(results.items())):
        raw = receipt.canonical.get("value")
        if not receipt.ok or receipt.call.name != "browser_snapshot" or not isinstance(raw, dict):
            continue
        elements = raw.get("elements")
        if not isinstance(elements, list):
            continue
        for row in elements:
            if not isinstance(row, dict) or not isinstance(known := row.get("ref"), str) or not known:
                continue
            if supplied == known:
                reason = "stale_ref"
                message = "Previously observed browser reference is stale or unavailable. Take a fresh browser_snapshot."
            elif supplied.startswith(f"[{known}] "):
                reason = "invalid_ref_format"
                message = ("A snapshot display row was supplied instead of its ref field. Choose an offered structured "
                           "ref or copy only the ref field from a current observation. Do not resubmit the display row.")
            else:
                continue
            return replace(output, model_text=f"ERROR: {message} The action was not delivered.",
                observation={**value, "reason": reason, "reference_receipt": receipt_ref})
    return output
