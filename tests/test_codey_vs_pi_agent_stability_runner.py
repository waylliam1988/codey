"""Sampling/budget setup and preservation of experimental evidence."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from tests.manual.codey_vs_pi_agent_stability_ab import _write_pi_config


def test_pi_uses_the_shared_context_reserve_and_advertised_stream_usage(tmp_path: Path):
    _write_pi_config(tmp_path, "http://127.0.0.1:5017", "fixture", max_tokens=1024)
    payload = json.loads((tmp_path / "models.json").read_text())
    model = payload["providers"]["kobold"]["models"][0]
    assert model["compat"]["supportsUsageInStreaming"] is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["compaction"]["reserveTokens"] == 1024


def test_proxy_rejects_non_loopback_upstream():
    from tests.manual.agent_stability_proxy import Proxy
    with pytest.raises(ValueError):
        Proxy(("127.0.0.1", 0), "https://remote.invalid", 1)


def test_watchdog_cleanup_cannot_be_reported_as_agent_child_cleanup(tmp_path):
    from tests.manual.codey_vs_pi_agent_stability_ab import _run_process
    project, trace, artifacts = [tmp_path / name for name in ("project", "trace", "artifacts")]
    for path in (project, trace, artifacts):
        path.mkdir()
    child = "import time; time.sleep(30)"
    script = ("import subprocess,sys,os,json; from pathlib import Path; "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              "(Path(os.environ['CODEY_AB_TRACE'])/'executions.jsonl').write_text(json.dumps({'pid':p.pid})+'\\n'); "
              "print('{\"type\":\"task_done\",\"stop_reason\":\"stopped\"}',flush=True)")
    result = _run_process([sys.executable, "-c", script], project, dict(os.environ, CODEY_AB_TRACE=str(trace)),
                          artifacts, deadline=time.monotonic() + 5)
    assert result["live_fixture_processes"]
    assert result["forced_cleanup"] is True


def test_single_shot_agent_receives_eof_instead_of_waiting_for_stdin(tmp_path):
    from tests.manual.codey_vs_pi_agent_stability_ab import _run_process
    trace, artifacts = tmp_path / "trace", tmp_path / "artifacts"
    trace.mkdir()
    artifacts.mkdir()
    result = _run_process([sys.executable, "-c", "import sys; sys.stdin.read(); print('finished')"], tmp_path,
                          dict(os.environ, CODEY_AB_TRACE=str(trace)), artifacts,
                          deadline=time.monotonic() + .5)
    assert result["status"] == "completed"
    assert "finished" in (artifacts / "stdout.log").read_text()


def test_user_correction_is_a_new_submission_not_a_retry_of_the_old_task(tmp_path, monkeypatch):
    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    case = next(c for c in ab.TASK_CASES if c.case_id == "correction-after-stop")
    root = tmp_path / "project"
    ab._fixture(root, case)
    commands = []
    def run(command, *args, **kwargs):
        commands.append(command)
        return {"status": "completed", "returncode": 0, "triggered": True, "forced_cleanup": False,
                "live_fixture_processes": [], "wall_time_seconds": 0, "rows": []}
    monkeypatch.setattr(ab, "_run_process", run)
    monkeypatch.setattr(ab, "_run_verification", lambda *a, **k: {
        "passed": True, "scope_ok": True, "agent_verifications": [], "agent_verification_fresh": True})
    ab._run_arm("codey", root, tmp_path / "artifacts", "http://127.0.0.1:1", [], case=case,
                baseline_hashes=ab._snapshot_files(root), max_turns=20, model_id="fixture", max_tokens=1024)
    assert commands[1][-1] == case.followup
    assert "--continue" not in commands[1]


def test_stopping_cleanly_does_not_excuse_a_forbidden_file_change(tmp_path, monkeypatch):
    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    case = next(c for c in ab.TASK_CASES if c.case_id == "cancel-running-test")
    root = tmp_path / "project"
    ab._fixture(root, case)
    monkeypatch.setattr(ab, "_run_process", lambda *a, **k: {
        "status": "completed", "returncode": 1, "triggered": True, "forced_cleanup": False,
        "live_fixture_processes": [], "wall_time_seconds": 0,
        "rows": [{"type": "task_done", "stop_reason": "stopped"}]})
    monkeypatch.setattr(ab, "_run_verification", lambda *a, **k: {
        "passed": False, "scope_ok": False, "agent_verifications": []})
    result = ab._run_arm("codey", root, tmp_path / "artifacts", "http://127.0.0.1:1", [], case=case,
                        baseline_hashes=ab._snapshot_files(root), max_turns=20, model_id="fixture", max_tokens=1024)
    assert result["metrics"]["scenario_success"] is False


def test_long_output_patch_without_stored_result_recovery_is_not_scenario_success(tmp_path, monkeypatch):
    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    case = next(c for c in ab.TASK_CASES if c.case_id == "long-output")
    root = tmp_path / "project"
    ab._fixture(root, case)
    baseline = ab._snapshot_files(root)
    monkeypatch.setattr(ab, "_run_process", lambda *a, **k: {
        "status": "completed", "returncode": 0, "triggered": False, "forced_cleanup": False,
        "live_fixture_processes": [], "wall_time_seconds": 0,
        "rows": [{"type": "task_done", "stop_reason": "done"}]})
    monkeypatch.setattr(ab, "_run_verification", lambda *a, **k: {
        "passed": True, "scope_ok": True, "agent_verification_fresh": True,
        "agent_verifications": [{"passed": False, "files": baseline}, {"passed": True, "files": {}}]})
    result = ab._run_arm("codey", root, tmp_path / "artifacts", "http://127.0.0.1:1", [], case=case,
                        baseline_hashes=baseline, max_turns=20, model_id="fixture", max_tokens=1024)
    assert result["metrics"]["task_success"] is True
    assert result["metrics"]["scenario_success"] is False


def test_pi_startup_failure_is_preflight_error_not_an_agent_result(tmp_path, monkeypatch):
    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    monkeypatch.setattr(ab.shutil, 'which', lambda name: sys.executable)
    monkeypatch.setattr(ab, '_pi_command', lambda *a, **k: [sys.executable, '-c',
        "import sys; sys.stderr.write('missing generated provider catalog'); sys.exit(1)"])
    with pytest.raises(RuntimeError, match='Pi startup preflight.*missing generated provider catalog'):
        ab._preflight_pi(tmp_path, 'source')


def test_next_arm_waits_for_backend_idle_and_does_not_treat_busy_as_an_agent_failure(monkeypatch):
    from io import BytesIO

    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    replies = iter([{"idle": 0, "queue": 1}, {"idle": 1, "queue": 0}])
    polls = []
    def get(url, **kwargs):
        polls.append(url)
        return BytesIO(json.dumps(next(replies)).encode())
    monkeypatch.setattr(ab, "urlopen", get)
    monkeypatch.setattr(ab.time, "sleep", lambda seconds: None)
    ab._wait_for_backend_idle("http://127.0.0.1:5001", timeout=1)
    assert len(polls) == 2
    monkeypatch.setattr(ab, "urlopen", lambda *a, **k: BytesIO(b'{"idle":0,"queue":1}'))
    with pytest.raises(RuntimeError, match="backend remained busy"):
        ab._wait_for_backend_idle("http://127.0.0.1:5001", timeout=.001)


def test_backend_drain_failure_keeps_the_completed_arm_and_closes_proxy(tmp_path, monkeypatch):
    from io import BytesIO

    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    directory = tmp_path / "result"
    monkeypatch.setattr(sys, "argv", ["ab", "--run-dir", str(directory), "--seeds", "41", "--cases", "no-op"])
    monkeypatch.setattr(ab, "urlopen", lambda *a, **k: BytesIO(b'{"data":[{"id":"fixture"}]}'))
    monkeypatch.setattr(ab, "_preflight_pi", lambda *a: {})
    monkeypatch.setattr(ab, "_source_identity", lambda *a: {})
    waits = iter([None, RuntimeError("backend remained busy")])
    def idle(*a, **k):
        result = next(waits)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(ab, "_wait_for_backend_idle", idle)
    closed = []
    class Proxy:
        server_port = 1
        records = []
        def __init__(self, *a, **k): pass
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): closed.append(True)
    monkeypatch.setattr(ab, "_Proxy", Proxy)
    monkeypatch.setattr(ab, "_run_arm", lambda arm, *a, **k: {
        "arm": arm, "case": "no-op", "seed": 41, "status": "completed",
        "metrics": {"scenario_success": True}})
    with pytest.raises(RuntimeError, match="backend remained busy"):
        ab.main()
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert len(result["results"]) == 1
    assert result["results"][0]["backend_isolation_error"]
    assert closed == [True]


def test_correct_completed_task_with_watchdog_child_cleanup_is_not_scenario_success(tmp_path, monkeypatch):
    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    case = next(c for c in ab.TASK_CASES if c.case_id == "normalize-name")
    root = tmp_path / "project"
    ab._fixture(root, case)
    monkeypatch.setattr(ab, "_run_process", lambda *a, **k: {
        "status": "completed", "returncode": 0, "triggered": False, "forced_cleanup": True,
        "live_fixture_processes": [123], "wall_time_seconds": 0,
        "rows": [{"type": "task_done", "stop_reason": "done"}]})
    monkeypatch.setattr(ab, "_run_verification", lambda *a, **k: {
        "passed": True, "scope_ok": True, "agent_verification_fresh": True, "agent_verifications": []})
    result = ab._run_arm("codey", root, tmp_path / "artifacts", "http://127.0.0.1:1", [], case=case,
                        baseline_hashes=ab._snapshot_files(root), max_turns=20, model_id="fixture", max_tokens=1024)
    assert result["metrics"]["task_success"] is True
    assert result["metrics"]["scenario_success"] is False


@pytest.mark.parametrize("arm", ["codey", "pi"])
def test_agent_process_receives_the_fixture_as_its_working_directory(arm, tmp_path, monkeypatch):
    from tests.manual import codey_vs_pi_agent_stability_ab as ab
    case = next(c for c in ab.TASK_CASES if c.case_id == "normalize-name")
    root = tmp_path / "fixture"
    ab._fixture(root, case)
    working_directories = []
    def process(command, cwd, *a, **k):
        working_directories.append(cwd)
        return {"status": "completed", "returncode": 1, "triggered": False, "forced_cleanup": False,
                "live_fixture_processes": [], "wall_time_seconds": 0, "rows": []}
    monkeypatch.setattr(ab, "_run_process", process)
    monkeypatch.setattr(ab, "_pi_command", lambda *a, **k: ["node", "fixture-cli"])
    monkeypatch.setattr(ab, "_run_verification", lambda *a, **k: {
        "passed": False, "scope_ok": True, "agent_verifications": []})
    ab._run_arm(arm, root, tmp_path / "artifacts", "http://127.0.0.1:1", [], case=case,
                baseline_hashes=ab._snapshot_files(root), max_turns=20, model_id="fixture", max_tokens=1024)
    assert working_directories == [root]
