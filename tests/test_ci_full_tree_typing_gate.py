"""The clean full tree must stay gated on both Windows and Linux."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


@pytest.fixture
def jobs():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]


@pytest.mark.parametrize("job_id", ["test", "test-linux"])
@pytest.mark.parametrize("command", ["python -m mypy codey", "pyrefly check codey", "ty check codey"])
def test_every_typechecker_covers_the_full_tree_for_each_job_version(jobs, job_id, command):
    job = jobs[job_id]
    if job_id == "test":
        assert set(job["strategy"]["matrix"]["python-version"]) == {"3.11", "3.12", "3.13"}
        assert job["strategy"]["fail-fast"] is False
        version = "${{ matrix.python-version }}"
    else:
        setup = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
        version = setup["with"]["python-version"]
    assert "if" not in job
    assert not job.get("continue-on-error", False)
    steps = [step for step in job["steps"] if step.get("run", "").startswith(command)]
    assert len(steps) == 1
    step = steps[0]
    assert step["run"] == f"{command} --python-version {version}"
    assert "if" not in step
    assert not step.get("continue-on-error", False)


@pytest.mark.parametrize("tool", ["mypy", "pyrefly", "ty"])
def test_each_ci_typechecker_has_a_pinned_install(tool):
    requirements = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(f"{tool}==") for line in requirements)


def test_windows_dependency_install_stops_at_the_first_failed_command(jobs):
    step = next(step for step in jobs["test"]["steps"] if step["name"] == "Install Python dependencies")
    assert step["shell"] == "pwsh"
    lines = step["run"].splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.startswith("python "):
            assert lines[index + 1] == "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
