"""The clean full tree must stay gated on both Windows and Linux."""

from pathlib import Path


def test_both_ci_jobs_check_the_full_dependency_tree():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.count("run: python -m mypy codey") == 2
    assert "--follow-imports=skip" not in workflow
