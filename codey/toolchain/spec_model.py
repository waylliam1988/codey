"""Tool declaration values and immutable parameter schemas."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from codey.toolchain.result_schema import text_output_schema
from codey.toolchain.schema_validation import _valid_custom_schema
from codey.toolchain.schema_validation import thaw_schema_value as thaw_schema_value
from codey.types.json_value import JsonObject, canonical_json_object

EXECUTION_CLAIMS_GUIDANCE = (
    "Optional execution_claims must match actual result_ref, status, exit_code_scope "
    "and observed exit_code from the receipts; omit exit_code when unknown. "
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    aliases: tuple[str, ...] = ()
    grant: str = ""
    parameters: tuple[tuple[str, object], ...] = ()
    required: tuple[str, ...] = ()
    json_examples: tuple[str, ...] = ()
    description: str = ""
    executor: str = ""
    replay_class: str = "unsafe"
    parallel_safe: bool = False
    supports_background: bool = False
    output_schema: Mapping[str, object] = field(default_factory=text_output_schema)
    declarations: str = ""
    namespace: str = "core"
    effect: str = "read"
    evidence_route: str = "none"
    preconditions: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        """Acquiring source data can be read-only without being safe to replay."""
        return (self.replay_class == "safe" or self.executor == "source" and self.grant == "web.read"
                or self.executor == "browser" and self.effect == "read")

    @property
    def parallel_project_read(self) -> bool:
        return self.executor == "project" and self.grant == "project.read" and self.parallel_safe and self.replay_class == "safe"


def _freeze_schema_value(value: object) -> object:
    """Freeze every nested schema mapping and sequence."""
    if isinstance(value, Mapping):
        mapping: Mapping[object, object] = value
        return MappingProxyType({key: _freeze_schema_value(item) for key, item in mapping.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_schema_value(item) for item in value)
    return value


def freeze_spec_parameters(spec: ToolSpec) -> ToolSpec:
    """Return a copy of ``spec`` with nested parameter schemas frozen."""
    if type(spec.supports_background) is not bool:
        raise ValueError(f"invalid background capability for {spec.name}")
    if not _valid_custom_schema(thaw_schema_value(spec.output_schema)):
        raise ValueError(f"invalid output schema for {spec.name}")
    try:
        params = tuple(
            (key, _freeze_schema_value(schema))
            for key, schema in spec.parameters
        )
    except Exception as exc:
        raise RuntimeError(f"tool snapshot freeze failed for {getattr(spec, 'name', '?')}: {exc}") from exc
    return replace(spec, parameters=params, output_schema=MappingProxyType(
        {key: _freeze_schema_value(value) for key, value in spec.output_schema.items()}))


def schema_for_spec(spec: ToolSpec) -> JsonObject:
    properties: JsonObject = {}
    for param_name, schema in spec.parameters:
        properties[param_name] = canonical_json_object(schema) if isinstance(schema, Mapping) else {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(spec.required),
        "additionalProperties": False,
    }
