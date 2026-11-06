from types import SimpleNamespace

from tests.manual.research_window_coverage_candidate import _coverage_order


def test_coverage_order_keeps_object_version_and_limit_windows_first():
    passages = {
        "a": SimpleNamespace(excerpt="V1 object alpha", source=SimpleNamespace(title="V1 specification")),
        "b": SimpleNamespace(excerpt="V2 object beta", source=SimpleNamespace(title="V2 specification")),
        "c": SimpleNamespace(excerpt="limitations and exceptions", source=SimpleNamespace(title="limits")),
    }
    session = SimpleNamespace(
        task_text="Compare object versions V1 and V2, including limitations and exceptions.",
        _memory_results={},
    )
    ledger = SimpleNamespace(read_passages=passages)
    scope = SimpleNamespace(aliases=(("e1", "a"), ("e2", "b"), ("e3", "c")))
    order = _coverage_order(session, ledger, scope)
    assert [next(ref for ref in row if ref in passages) for row in order[:3]] == ["a", "c", "b"]
