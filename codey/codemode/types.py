from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypedDict

from codey.protocols.codemode import freeze_codemode_mapping
from codey.toolchain.result_schema import text_output_schema


class DeliveryReference(TypedDict):
    call_id: str
    child_index: int | None


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
        object.__setattr__(self, "parameters", freeze_codemode_mapping(self.parameters))
        object.__setattr__(self, "output_schema", freeze_codemode_mapping(self.output_schema))


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    entries: tuple[ToolCatalogEntry, ...]
    digest: str

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
