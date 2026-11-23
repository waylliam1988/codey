from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypedDict

from codey.toolchain.result_schema import text_output_schema
from codey.types.json_value import JsonValue, canonical_json_object


class DeliveryReference(TypedDict):
    call_id: str
    child_index: int | None


def _freeze_value(value: JsonValue) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_value(item) for key, item in canonical_json_object(value).items()})


@dataclass(frozen=True)
class CodeModeLimits:
    """One attempt's worker limits; child positions span the logical invocation.

    Replayed positions consume their original receipts, not new effect budget.
    timeout_ms bounds each worker attempt and inherits any outer deadline.
    """
    source_bytes: int = 100_000
    timeout_ms: int = 30_000
    max_child_calls: int = 64
    max_concurrent_calls: int = 8
    max_output_bytes: int = 200_000
    max_pending_promises: int = 64
    max_rpc_bytes: int = 1_000_000

    def as_dict(self) -> dict[str, int]:
        return {
            "source_bytes": self.source_bytes,
            "timeout_ms": self.timeout_ms,
            "max_child_calls": self.max_child_calls,
            "max_concurrent_calls": self.max_concurrent_calls,
            "max_output_bytes": self.max_output_bytes,
            "max_pending_promises": self.max_pending_promises,
            "max_rpc_bytes": self.max_rpc_bytes,
        }

    def __post_init__(self) -> None:
        for field_name, value in self.as_dict().items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_concurrent_calls > self.max_child_calls:
            raise ValueError("max_concurrent_calls cannot exceed max_child_calls")


@dataclass(frozen=True)
class CodeModeProgram:
    code: str
    language: str = "javascript"
    inputs: Mapping[str, object] = field(default_factory=dict)
    repair_of: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _freeze_mapping(self.inputs))
        object.__setattr__(self, "repair_of", str(self.repair_of or "").strip())


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    description: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    namespace: str = ""
    read_only: bool = True
    parallel_safe: bool = False
    grant: str = ""
    output_schema: Mapping[str, object] = field(default_factory=text_output_schema)

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("catalog tool name must be non-empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters))
        object.__setattr__(self, "output_schema", _freeze_mapping(self.output_schema))


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    entries: tuple[ToolCatalogEntry, ...]
    digest: str

    def summary(self, *, budget: int = 2_000) -> CatalogSummary:
        from codey.codemode.catalog import catalog_summary

        return catalog_summary(self, budget=budget)

    def search(
        self, query: str = "", *, namespace: str | None = None, limit: int = 10, offset: int = 0,
    ) -> CatalogSearchResult:
        from codey.codemode.catalog import catalog_search

        return catalog_search(self, query, namespace=namespace, limit=limit, offset=offset)

    def describe(self, path: str) -> CatalogSearchItem | None:
        from codey.codemode.catalog import catalog_describe

        return catalog_describe(self, path)


@dataclass(frozen=True)
class CatalogSearchItem:
    path: str
    signature: str
    description: str


@dataclass(frozen=True)
class CatalogSearchResult:
    items: tuple[CatalogSearchItem, ...]
    remaining: int
    next_offset: int | None


@dataclass(frozen=True)
class CatalogNamespaceSummary:
    name: str
    count: int
    entries: tuple[CatalogSearchItem, ...]


@dataclass(frozen=True)
class CatalogSummary:
    total: int
    shown: int
    namespaces: tuple[CatalogNamespaceSummary, ...]
    text: str


@dataclass(frozen=True)
class CodeModeDiagnostic:
    code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CodeModeResult:
    ok: bool
    value: object = None
    output: str = ""
    error_code: str = ""
    error: str = ""
    child_calls: int = 0
    diagnostics: tuple[CodeModeDiagnostic, ...] = ()
