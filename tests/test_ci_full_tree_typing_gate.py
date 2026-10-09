"""The clean full tree must stay gated on both Windows and Linux."""

from pathlib import Path


def test_both_ci_jobs_check_the_full_dependency_tree():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.count("run: python -m mypy codey") == 2
    assert "--follow-imports=skip" not in workflow


def test_pyrefly_checks_the_full_tree_without_a_baseline():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "run: pyrefly check codey\n" in workflow
    assert "--baseline" not in workflow
    assert "pyrefly==1.3.2" in (root / "requirements-ci.txt").read_text(encoding="utf-8")
