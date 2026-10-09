"""Held-out correctness and actual agent test execution are separate evidence."""

from pathlib import Path

from tests.manual.codey_vs_pi_agent_stability_ab import _fixture, _run_verification


def test_stability_suite_includes_noop_faults_cancellation_and_resume():
    from tests.manual.codey_vs_pi_agent_stability_ab import TASK_CASES
    assert {"no-op", "test-first", "scope-blocked", "long-output", "multi-file", "http-503",
            "truncated-call", "cancel-running-test", "resume-after-edit", "correction-after-stop"} <= {
                case.case_id for case in TASK_CASES}


def test_agent_verification_is_fresh_only_for_the_final_file_contents(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    from tests.manual.codey_vs_pi_agent_stability_ab import TASK_CASES
    case = next(case for case in TASK_CASES if case.case_id == "normalize-name")
    trace = tmp_path / "trace"
    trace.mkdir()
    project = tmp_path / "project"
    _fixture(project, case)
    (project / "app.py").write_text(
        "import re\ndef normalize_name(value): return '-'.join(re.sub(r'[^a-z0-9\\s]', '', value.lower()).split())\n",
        encoding="utf-8")
    env = dict(os.environ, CODEY_AB_TRACE=str(trace))
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=project, env=env, check=True,
                   capture_output=True)
    monkeypatch.setenv("CODEY_AB_TRACE", str(trace))
    assert _run_verification(project, case=case)["agent_verification_fresh"] is True
    with (project / "app.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# changed after verification\n")
    assert _run_verification(project, case=case)["agent_verification_fresh"] is False


def test_hardcoding_the_two_visible_examples_fails_held_out_checks(tmp_path: Path):
    _fixture(tmp_path)
    (tmp_path / "app.py").write_text("def normalize_name(value): return 'hello-world'\n", encoding="utf-8")
    result = _run_verification(tmp_path)
    assert result["visible_tests"] is True
    assert result["hidden_checks"] is False


def test_external_verification_is_not_recorded_as_agent_execution(tmp_path: Path):
    _fixture(tmp_path)
    result = _run_verification(tmp_path)
    assert result["agent_verification_fresh"] is False
