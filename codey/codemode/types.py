from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True)
class CodeModeLimits:
    source_bytes: int = 100_000
    timeout_ms: int = 30_000
    max_child_calls: int = 64
    max_concurrent_calls: int = 8
    max_output_bytes: int = 200_000
    max_pending_promises: int = 64

    def __post_init__(self) -> None:
        for field_name in (
            "source_bytes",
            "timeout_ms",
            "max_child_calls",
            "max_concurrent_calls",
            "max_output_bytes",
            "max_pending_promises",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_concurrent_calls > self.max_child_calls:
            raise ValueError("max_concurrent_calls cannot exceed max_child_calls")


@dataclass(frozen=True)
class CodeModeProgram:
    code: str
    language: str = "javascript"


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    description: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    namespace: str = ""
    read_only: bool = True
    parallel_safe: bool = False
    grant: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("catalog tool name must be non-empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", str(self.description or ""))
        frozen = _freeze_value(self.parameters)
        object.__setattr__(self, "parameters", frozen if isinstance(frozen, Mapping) else MappingProxyType({}))


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    entries: tuple[ToolCatalogEntry, ...]
    digest: str


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
