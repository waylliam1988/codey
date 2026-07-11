from __future__ import annotations

from tests.manual import large_project_ab


def test_large_project_baseline_omits_navigation_tools_only() -> None:
    prompt = large_project_ab.baseline_prompt()

    assert "outline_file" not in prompt
    assert "find_references" not in prompt
    assert '{"tool":"grep","args":{"query":"login handler"' in prompt
    assert "regex is not supported" in prompt


def test_large_project_benchmark_mutations_are_hard_disabled() -> None:
    outcome = large_project_ab._read_only_error()

    assert not outcome.ok
    assert not outcome.changed
    assert "read-only" in outcome.output
