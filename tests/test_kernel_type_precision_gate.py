from __future__ import annotations

import json

import pytest

from tools.kernel_type_precision_gate import BASELINE, current_counts, validate_counts


def test_kernel_type_precision_does_not_regress() -> None:
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert current_counts() == expected


def test_kernel_type_precision_rejects_a_new_dynamic_escape() -> None:
    expected = {"codey/operations/kernel_execution.py": 8}
    with pytest.raises(SystemExit, match="precision changed"):
        validate_counts(expected, {"codey/operations/kernel_execution.py": 9})
