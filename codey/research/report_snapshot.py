"""Read-only report records decoded by the existing report wire codecs."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias, overload

from codey.research import report_values
from codey.types.json_value import JsonObject, JsonPrimitive, JsonValue, canonical_json_object, require_json_string

FrozenValue: TypeAlias = JsonPrimitive | Mapping[str, "FrozenValue"] | tuple["FrozenValue", ...]


def _freeze(value: JsonValue) -> FrozenValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, eq=False, init=False)
class ReportRecord(Mapping[str, object]):
    _values: Mapping[str, FrozenValue]

    def __init__(self, raw: object) -> None:
        supplied = canonical_json_object(raw)
        decoded = self._decode(supplied)
        if decoded != supplied:
            raise ValueError("report contains noncanonical or unknown fields")
        object.__setattr__(self, "_values", MappingProxyType({key: _freeze(value) for key, value in decoded.items()}))

    def _decode(self, raw: JsonObject) -> JsonObject:
        raise NotImplementedError

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ReportRecord):
            return self._values == other._values
        if isinstance(other, Mapping):
            return self.to_payload() == canonical_json_object(other)
        return NotImplemented

    def to_payload(self) -> JsonObject:
        return canonical_json_object(self._values)


class ReportCitation(ReportRecord):
    def _decode(self, raw: JsonObject) -> JsonObject:
        return canonical_json_object(report_values.citation_binding(raw))

    @overload
    def __getitem__(self, key: Literal["label", "passage_ref"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["start", "end"]) -> int: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class ReportReference(ReportRecord):
    def _decode(self, raw: JsonObject) -> JsonObject:
        return canonical_json_object(report_values.version_reference(raw))

    @overload
    def __getitem__(self, key: Literal["id"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["revision"]) -> int: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class ReportSource(ReportRecord):
    def _decode(self, raw: JsonObject) -> JsonObject:
        return canonical_json_object(report_values.source_version(raw))

    @overload
    def __getitem__(self, key: Literal["id", "title", "url", "content_hash", "retrieved_at",
                                     "acquisition", "publication_date", "release_version"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["revision", "number"]) -> int: ...
    @overload
    def __getitem__(self, key: Literal["quality"]) -> Mapping[str, str]: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class ReportEvidence(ReportRecord):
    def _decode(self, raw: JsonObject) -> JsonObject:
        return canonical_json_object(report_values.evidence_binding(raw))

    @overload
    def __getitem__(self, key: Literal["label", "evidence_id", "source_id", "excerpt", "excerpt_digest",
                                     "locator", "relation", "claim_id", "acquisition", "passage_ref"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["evidence_revision", "source_revision", "claim_revision", "start", "end"]) -> int: ...
    @overload
    def __getitem__(self, key: Literal["page"]) -> int | None: ...
    @overload
    def __getitem__(self, key: Literal["display_truncated"]) -> bool: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class ReportMutation(ReportRecord):
    def _decode(self, raw: JsonObject) -> JsonObject:
        return canonical_json_object(report_values.mutation_payload(raw))

    @overload
    def __getitem__(self, key: Literal["value"]) -> Mapping[str, FrozenValue]: ...
    @overload
    def __getitem__(self, key: Literal["expected_revision"]) -> int: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class ReportKnowledgeBatch(ReportRecord):
    def _decode(self, raw: JsonObject) -> JsonObject:
        if set(raw) != {"updates", "scope", "scope_id", "session_id"} or not isinstance(raw["updates"], list):
            raise ValueError("invalid frozen knowledge batch")
        for key in ("scope", "scope_id", "session_id"):
            require_json_string(raw[key], key)
        # The report validates updates against its selected evidence labels.
        return raw

    @overload
    def __getitem__(self, key: Literal["scope", "scope_id", "session_id"]) -> str: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)
