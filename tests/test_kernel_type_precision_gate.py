from __future__ import annotations

import json

from tools.kernel_type_precision_gate import BASELINE, current_counts


def test_kernel_type_precision_does_not_regress() -> None:
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert current_counts() == expected
