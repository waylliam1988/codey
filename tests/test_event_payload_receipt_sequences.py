"""Keep receipt reference ordering and per-field bounds during codec cleanup."""
from __future__ import annotations

import pytest

from codey.app.event_payloads import bounded_receipt


class _IndexedReferences:
    def __init__(self, rows: list[str]) -> None:
        self.rows = rows

    def __getitem__(self, index: int) -> str:
        return self.rows[index]


@pytest.mark.parametrize("kind", ["list", "tuple", "indexed"])
def test_receipt_reference_sequences_keep_order_and_field_specific_limits(kind: str) -> None:
    def values(rows: list[str]) -> list[str] | tuple[str, ...] | _IndexedReferences:
        if kind == "indexed":
            return _IndexedReferences(rows)
        return tuple(rows) if kind == "tuple" else rows

    receipt: dict[str, object] = {
        "schema_version": 1,
        "verification": {"trust": "trusted", "checks_passed": False,
                         "proof_refs": values(["", "proof1", "proof2", "proof3"])},
        "integrity": {"status": "clean", "severity": "none",
                      "reason_codes": values(["", "one", "two", "three"]),
                      "affected_paths": values(["", "a.py", "b.py", "c.py", "d.py", "e.py"]),
                      "refs": values(["", "r1", "r2", "r3", "r4", "r5"])},
    }
    assert bounded_receipt(receipt) == {
        "schema_version": 1,
        "verification": {"trust": "trusted", "checks_passed": False, "proof_refs": ["proof1", "proof2"]},
        "integrity": {"status": "clean", "severity": "none", "reason_codes": ["one", "two", "three"],
                      "affected_paths": ["a.py", "b.py", "c.py", "d.py"], "refs": ["r1", "r2", "r3", "r4"]},
    }
