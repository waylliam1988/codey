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


def test_scoped_gate_ignores_unmigrated_neighbors_but_checks_imported_aliases() -> None:
    source = '''from typing import Any as Dynamic
Alias = Dynamic
def legacy(value: Dynamic) -> Dynamic: return value
def protected(value: str) -> str: return value
'''
    assert not source_violations(source, symbols=("protected",))
    assert source_violations(source.replace("value: str", "value: Alias"), symbols=("protected",))


def test_scoped_gate_checks_dataclass_fields_individually() -> None:
    source = '''from typing import Any
class Frame:
    provider: Any
    entry_policy: "TaskPolicy | None" = None
'''
    assert not source_violations(source, symbols=("Frame.entry_policy",))
    assert source_violations(source.replace('"TaskPolicy | None"', "Any"), symbols=("Frame.entry_policy",))


def test_scoped_gate_rejects_removed_contracts() -> None:
    assert source_violations("class Frame: pass", symbols=("Frame.entry_policy",)) == [
        "missing protected symbol: Frame.entry_policy",
    ]


def test_scoped_gate_checks_nested_callback_annotations() -> None:
    source = '''from typing import Any
def protected(value: str) -> str:
    def mutation(projection: Any) -> str:
        return value
    return value
'''
    assert source_violations(source, symbols=("protected",))


@pytest.mark.parametrize("source", [
    'from collections.abc import Callable\ndef f(callback: Callable[..., object]) -> None: pass',
    'from typing import Callable as Callback\ndef f(callback: "Callback[..., str]") -> None: pass',
    'from typing import TypeAlias\nfrom collections.abc import Callable\nRowsBuilder: TypeAlias = Callable[..., str]',
])
def test_closed_callback_gate_rejects_unspecified_parameters(source: str) -> None:
    assert source_violations(source, closed_callables=True)


def test_closed_callback_gate_allows_exact_signatures() -> None:
    assert not source_violations('''from collections.abc import Callable
from typing import TypeAlias
RowsBuilder: TypeAlias = Callable[[str, tuple[int, ...]], tuple[str, ...]]
def f(callback: RowsBuilder) -> None: pass
''', closed_callables=True)
