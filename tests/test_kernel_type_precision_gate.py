from __future__ import annotations

import json

import pytest

from tools.kernel_type_precision_gate import BASELINE, current_counts, source_violations, validate_counts


def test_kernel_type_precision_does_not_regress() -> None:
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert current_counts() == expected


def test_kernel_type_precision_rejects_a_new_dynamic_escape() -> None:
    expected = {"codey/operations/kernel_execution.py": 8}
    with pytest.raises(SystemExit, match="precision changed"):
        validate_counts(expected, {"codey/operations/kernel_execution.py": 9})


@pytest.mark.parametrize("source", [
    'import typing as t\ndef f(x: t.Any) -> int: return 1',
    'from typing import Any as Dynamic\ndef f(x: "list[Dynamic]") -> int: return 1',
    'from typing import Any, TypeAlias\nEscape: TypeAlias = "Any"\ndef f(x: Escape) -> int: return 1',
    'from typing import Any\nAlias = Any\ndef f(x: Alias) -> int: return 1',
    'def f(x: dict) -> list: return []',
    'from typing import List as Bag\ndef f(x: Bag) -> int: return 1',
    'def f(x): return x',
    'def __getattr__(name: str) -> object: return None',
    'def send(*args: object, **kwargs: object) -> object: return None',
    'def f(provider: object) -> object: return getattr(provider, "send")()',
    'def f(state: object) -> object: return state',
    'from typing import cast\ndef f(x: object) -> object: return cast("typing.Any", x)',
    'import typing as t\ndef f(x: object) -> object: return t.cast("t.Any", x)',
    'from typing import cast as co, Any as A\ndef f(x: object) -> object: return co("A", x)',
])
def test_rejects_type_and_dynamic_dispatch_escapes(source):
    assert source_violations(source)


def test_literal_data_and_valid_typed_boundaries_are_not_escapes():
    assert not source_violations('''from typing import Literal
message: str = "Any"
def decode(raw: object, kind: Literal["Any", "list"]) -> dict[str, str]:
    return {"kind": kind}
''')


def test_adapter_can_accept_raw_provider_and_finite_dynamic_calls():
    source = 'def probe(provider: object) -> object: return getattr(provider, "send", None)'
    assert not source_violations(source, core=False)
