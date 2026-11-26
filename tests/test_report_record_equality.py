"""Mapping equality does not weaken report construction or hide internal errors."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType

import pytest

from codey.research.report_snapshot import ReportCitation
from codey.types.json_value import JsonObject

PAYLOAD: JsonObject = {"label": "e1", "passage_ref": "p1", "start": 0, "end": 1}


@pytest.mark.parametrize("invalid", [{1: "x"}, {"a": object()}, {"a": float("nan")}])
def test_invalid_external_mapping_compares_false(invalid: object) -> None:
    record = ReportCitation(PAYLOAD)
    assert (record == invalid) is False
    assert (invalid == record) is False
    with pytest.raises((TypeError, ValueError)):
        ReportCitation(invalid)


def test_cyclic_mapping_compares_false() -> None:
    cycle: dict[str, object] = {}
    cycle["cycle"] = cycle
    assert (ReportCitation(PAYLOAD) == cycle) is False


def test_mapping_type_error_is_not_an_equality_failure() -> None:
    class InvalidMapping(Mapping[str, object]):
        def __iter__(self) -> Iterator[str]:
            raise TypeError("invalid external mapping")
        def __len__(self) -> int:
            return 1
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)
    assert (ReportCitation(PAYLOAD) == InvalidMapping()) is False
    with pytest.raises(TypeError, match="invalid external mapping"):
        ReportCitation(InvalidMapping())


def test_valid_records_and_mappings_preserve_equality() -> None:
    record = ReportCitation(PAYLOAD)
    assert record == PAYLOAD == record
    assert record == MappingProxyType(PAYLOAD)
    assert record == ReportCitation(PAYLOAD)
    assert record != ReportCitation({**PAYLOAD, "end": 2})
    assert record != {**PAYLOAD, "label": "e2"}
    assert record != object()


def test_non_mapping_preserves_reflected_comparison() -> None:
    class Reflected:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, ReportCitation)
    assert ReportCitation(PAYLOAD) == Reflected()


def test_internal_serialization_failure_is_not_hidden() -> None:
    class BrokenCitation(ReportCitation):
        def to_payload(self) -> JsonObject:
            raise ValueError("internal serialization failure")
    with pytest.raises(ValueError, match="internal serialization"):
        assert BrokenCitation(PAYLOAD) == PAYLOAD
