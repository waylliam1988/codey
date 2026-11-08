"""Local RAM-disk routing must remain opt-in and safe across processes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_RUN_ENV = "CODEY_PYTEST_RAMDISK_RUN"
_PROBE = r'''
import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from tests.support.pytest_temp import bootstrap

project, mode = Path(sys.argv[1]), sys.argv[2]
before = {key: os.environ.get(key) for key in ('TEMP', 'TMP', 'TMPDIR', 'CODEY_PYTEST_RAMDISK_RUN')}
cached = tempfile.gettempdir()
if mode == 'unwritable':
    with patch('tests.support.pytest_temp.tempfile.TemporaryFile', side_effect=PermissionError('read-only')):
        run = bootstrap(project)
elif mode == 'full':
    with patch('tests.support.pytest_temp.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
        run = bootstrap(project)
elif mode == 'unmounted':
    with patch.object(Path, 'is_dir', return_value=False):
        run = bootstrap(project)
else:
    run = bootstrap(project)
assert bootstrap(project) is run if mode not in ('unwritable', 'full', 'unmounted') else run is None
result = {'before': before, 'after': {key: os.environ.get(key) for key in before},
          'cached_before': cached, 'temp': tempfile.gettempdir(), 'root': str(run.root) if run else None}
if run:
    result['owner'] = run.owner_pid
    result['pid'] = os.getpid()
    if mode in ('nested', 'nested_failure'):
        child_code = """
import json, sys, tempfile
from pathlib import Path
from tests.support.pytest_temp import bootstrap
run = bootstrap(Path(sys.argv[1]))
run.finish(int(sys.argv[2]))
print(json.dumps({'root': str(run.root), 'temp': tempfile.gettempdir(), 'owner': run.owner_pid}))
"""
        child = subprocess.run([sys.executable, '-B', '-c', child_code, str(project),
                                '1' if mode == 'nested_failure' else '0'],
                               capture_output=True, text=True, check=True, timeout=30)
        result['child'] = json.loads(child.stdout)
        result['exists_after_child'] = run.root.is_dir()
    run.finish(1 if mode == 'failure' else 0)
print(json.dumps(result), flush=True)
if mode == 'wait':
    (project / ('ready-' + str(os.getpid()))).write_text(json.dumps(result), encoding='utf-8')
    sys.stdin.readline()
'''


def _environment(**updates: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in (_RUN_ENV, "PYTEST_STATE_HOME", "GITHUB_ACTIONS", "PYTEST_DEBUG_TEMPROOT", "PYTEST_ADDOPTS"):
        env.pop(key, None)
    env.update(updates)
    return env


def _configure(project: Path, base: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / ".pytest-local.toml").write_text(f"[ramdisk]\nroot = {json.dumps(str(base))}\n", encoding="utf-8")


def _probe(project: Path, mode: str = "success", **updates: str) -> tuple[dict, str]:
    result = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE, str(project), mode], cwd=_REPO,
        env=_environment(**updates), capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), result.stderr


@pytest.mark.parametrize("github", [False, True])
def test_no_local_opt_in_or_github_actions_preserves_temp_defaults(tmp_path, github):
    project, base = tmp_path / "project", tmp_path / "unused"
    project.mkdir()
    if github:
        _configure(project, base)
    data, errors = _probe(project, **({"GITHUB_ACTIONS": "true"} if github else {}))
    assert data["root"] is None
    assert data["before"] == data["after"]
    assert data["cached_before"] == data["temp"]
    assert not base.exists()
    assert not errors


@pytest.mark.parametrize("mode", ["success", "nested", "failure", "nested_failure"])
def test_cached_temp_is_redirected_children_share_and_only_owner_cleans(tmp_path, mode):
    project, base = tmp_path / "project", tmp_path / "ram 中文"
    _configure(project, base)
    data, errors = _probe(project, mode)
    root = Path(data["root"])
    assert root.parent == base.resolve()
    assert Path(data["temp"]) == root / "tmp"
    assert all(data["after"][key] == data["temp"] for key in ("TEMP", "TMP", "TMPDIR"))
    assert data["owner"] == data["pid"]
    if mode.startswith("nested"):
        assert data["child"]["root"] == data["root"]
        assert data["child"]["temp"] == data["temp"]
        assert data["child"]["owner"] == data["owner"]
        assert data["exists_after_child"]
    if "failure" in mode:
        assert root.is_dir()
        assert (root / ".failed").is_file()
    else:
        assert not root.exists()
    assert not errors


@pytest.mark.parametrize("mode", ["invalid", "relative", "blocked", "unwritable", "full", "unmounted"])
def test_initialization_failure_does_not_partially_change_temp_environment(tmp_path, mode):
    project, base = tmp_path / "project", tmp_path / "ram"
    _configure(project, base)
    if mode == "invalid":
        (project / ".pytest-local.toml").write_text("[ramdisk", encoding="utf-8")
    elif mode == "relative":
        (project / ".pytest-local.toml").write_text('[ramdisk]\nroot = "relative"\n', encoding="utf-8")
    elif mode == "blocked":
        base.write_text("not a directory", encoding="utf-8")
    data, errors = _probe(project, mode if mode in ("unwritable", "full", "unmounted") else "success")
    assert data["root"] is None
    assert data["before"] == data["after"]
    assert data["temp"] == data["cached_before"]
    assert "keeping the default temporary directory" in errors
    if base.is_dir():
        assert not list(base.glob("run-*"))


def test_independent_concurrent_processes_never_share_or_remove_each_others_run(tmp_path):
    project, base = tmp_path / "project", tmp_path / "ram"
    _configure(project, base)
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen(
                [sys.executable, "-B", "-c", _PROBE, str(project), "wait"], cwd=_REPO,
                env=_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 30
        ready = [project / f"ready-{process.pid}" for process in processes]
        while not all(path.exists() and path.stat().st_size for path in ready):
            assert time.monotonic() < deadline, "Concurrent temporary directories were not initialized"
            assert all(process.poll() is None for process in processes)
            time.sleep(0.02)
        second_root = Path(json.loads(ready[1].read_text(encoding="utf-8"))["root"])
        # communicate has a timeout; the second process remains alive while the first exits.
        first_output, first_errors = processes[0].communicate("done\n", timeout=45)
        assert processes[0].returncode == 0, first_errors
        first = json.loads(first_output)
        assert not Path(first["root"]).exists()
        assert second_root.is_dir()
        second_output, second_errors = processes[1].communicate("done\n", timeout=45)
        assert processes[1].returncode == 0, second_errors
        second = json.loads(second_output)
        assert first["root"] != second["root"]
        assert not Path(second["root"]).exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=15)


_INTEGRATION_TEST = '''
import json, os, subprocess, sys, tempfile
from pathlib import Path

def test_real_paths(tmp_path):
    child = subprocess.run([sys.executable, '-B', '-c',
        "import json,tempfile;print(json.dumps({'temp':tempfile.gettempdir(),'created':tempfile.mkdtemp()}))"],
        capture_output=True, text=True, check=True, timeout=30)
    print('RAM_PROBE=' + json.dumps({'tmp_path':str(tmp_path), 'temp':tempfile.gettempdir(),
        'home':str(Path.home()), 'state_home':os.environ['PYTEST_STATE_HOME'],
        'child':json.loads(child.stdout)}))
    assert os.environ.get('PROBE_FAIL') != '1'
'''


@pytest.mark.parametrize("mode", ["active", "absent", "github", "basetemp", "debugtemp", "statehome", "failure"])
def test_real_pytest_bootstrap_respects_overrides_and_ci_defaults(tmp_path, mode):
    project, base = tmp_path / "project 中文", tmp_path / "ram 中文"
    support = project / "tests" / "support"
    support.mkdir(parents=True)
    (project / "tests" / "__init__.py").touch()
    (support / "__init__.py").touch()
    shutil.copyfile(_REPO / "tests/conftest.py", project / "tests/conftest.py")
    shutil.copyfile(_REPO / "tests/support/pytest_temp.py", support / "pytest_temp.py")
    (project / "tests/test_probe.py").write_text(_INTEGRATION_TEST, encoding="utf-8")
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-p no:cacheprovider"\n', encoding="utf-8")
    if mode != "absent":
        _configure(project, base)
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    env = _environment(PYTHONPATH=str(_REPO), TEMP=str(system_temp), TMP=str(system_temp), TMPDIR=str(system_temp))
    args = [sys.executable, "-B", "-m", "pytest", "-s", "tests/test_probe.py"]
    explicit = tmp_path / "explicit"
    if mode == "github":
        env["GITHUB_ACTIONS"] = "true"
    elif mode == "basetemp":
        args.extend(["--basetemp", str(explicit)])
    elif mode == "debugtemp":
        explicit.mkdir()
        env["PYTEST_DEBUG_TEMPROOT"] = str(explicit)
    elif mode == "statehome":
        env["PYTEST_STATE_HOME"] = str(explicit)
    elif mode == "failure":
        env["PROBE_FAIL"] = "1"
    result = subprocess.run(args, cwd=project, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == (1 if mode == "failure" else 0), result.stdout + result.stderr
    record = next(line.split("RAM_PROBE=", 1)[1] for line in result.stdout.splitlines() if "RAM_PROBE=" in line)
    data = json.loads(record)
    temp = Path(data["temp"])
    assert data["child"]["temp"] == data["temp"]
    assert Path(data["child"]["created"]).parent == temp
    assert data["home"] == data["state_home"]
    if mode in ("absent", "github"):
        assert temp == system_temp.resolve()
        assert not base.exists()
        assert "RAM disk:" not in result.stdout
    else:
        root = temp.parent
        assert root.parent == base.resolve()
        if mode == "failure":
            assert root.is_dir()
            assert Path(data["home"]).is_dir()
            assert "retained after failure" in result.stdout
        else:
            assert not root.exists(), result.stdout + result.stderr
    expected_tmp = explicit if mode in ("basetemp", "debugtemp") else temp
    assert Path(data["tmp_path"]).is_relative_to(expected_tmp)
    if mode == "statehome":
        assert Path(data["home"]) == explicit.resolve()
        assert explicit.is_dir()
