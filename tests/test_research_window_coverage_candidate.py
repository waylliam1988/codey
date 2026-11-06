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
    # The helper returns eviction order. With no redundant window, the stable
    # source order is retained and no version is silently collapsed.
    assert [next(ref for ref in row if ref in passages) for row in order[:3]] == ["a", "b", "c"]


def test_eviction_does_not_remove_one_required_version_while_other_remains():
    passages = {
        "a": SimpleNamespace(excerpt="V1 object alpha", source=SimpleNamespace(title="V1 specification")),
        "b": SimpleNamespace(excerpt="V2 object beta", source=SimpleNamespace(title="V2 specification")),
        "c": SimpleNamespace(excerpt="limitations and exceptions", source=SimpleNamespace(title="limits")),
        "d": SimpleNamespace(excerpt="background object", source=SimpleNamespace(title="background")),
    }
    session = SimpleNamespace(task_text="Compare object versions V1 and V2, including limitations and exceptions.", _memory_results={})
    ledger = SimpleNamespace(read_passages=passages)
    scope = SimpleNamespace(aliases=tuple((f"e{i}", ref) for i, ref in enumerate(passages, 1)))
    order = _coverage_order(session, ledger, scope)
    labels = [next(ref for ref in row if ref in passages) for row in order]
    assert labels[0] == "d"
    assert set(labels[1:]) == {"a", "b", "c"}


def test_source_comparison_marks_each_observed_source_as_required():
    passages = {
        "a": SimpleNamespace(excerpt="requires token", source=SimpleNamespace(title="Deployment restrictions")),
        "b": SimpleNamespace(excerpt="fast preview does not measure production", source=SimpleNamespace(title="Feature overview")),
        "c": SimpleNamespace(excerpt="catalogue has no operational rule", source=SimpleNamespace(title="Release catalogue")),
    }
    session = SimpleNamespace(
        task_text="分别读取三类来源并列出来源对照：Deployment restrictions、Feature overview、Release catalogue。",
        _memory_results={},
    )
    ledger = SimpleNamespace(read_passages=passages)
    scope = SimpleNamespace(aliases=tuple((f"e{i}", ref) for i, ref in enumerate(passages, 1)))
    order = _coverage_order(session, ledger, scope)
    assert {next(ref for ref in row if ref in passages) for row in order} == set(passages)
