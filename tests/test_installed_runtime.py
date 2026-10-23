"""Build and install a real wheel, then launch outside the checkout."""
import os
import subprocess
import sys
import tarfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wheel_cold_start_reconnect_and_upgrade_without_repository(tmp_path):
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    env = {**os.environ, 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
    built = subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(ROOT), '--no-deps', '--no-build-isolation',
                            '--wheel-dir', str(wheels)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, (built.stdout + built.stderr)[-5000:]
    environment = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    installed = subprocess.run([str(python), '-m', 'pip', 'install', '--no-deps', '--no-index', '--ignore-installed',
                                str(next(wheels.glob('*.whl')))], cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=60)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    script = r'''
import json, sys, time
from pathlib import Path
import codey
from codey.control.client import ensure_service
from codey.launcher.runtime import read_runtime
root=Path(codey.__file__).resolve().parent.parent
assert root.is_relative_to(Path(sys.prefix)), root
assert not (root/'pyproject.toml').exists()
assert not (root/'tools/publish_runtime.py').exists()
assert read_runtime(root)['version']==codey.__version__
home=Path(sys.argv[1]);client=None
try:
    client=ensure_service(home,port=0)
    first=client.call('get_health')
    assert first['status']=='ready'
    assert ensure_service(home,port=0).call('get_health')['instance_id']==first['instance_id']
    # Simulate an installed package update, using the same published inventory.
    init=root/'codey/__init__.py'
    init.write_bytes(init.read_bytes()+b'\n# installed update probe\n')
    second=ensure_service(home,port=0).call('get_health')
    assert second['status']=='ready' and second['release_id']!=first['release_id']
    assert second['instance_id']!=first['instance_id']
    client.lifecycle('drain');client.lifecycle('shutdown')
    deadline=time.monotonic()+15
    while (home/'service.json').exists() and time.monotonic()<deadline:
        time.sleep(0.05)
    third=ensure_service(home,port=0).call('get_health')
    assert third['status']=='ready' and third['release_id']==second['release_id']
    assert third['instance_id']!=second['instance_id']
    print(json.dumps({'cold_start':'ready','reconnect':'same instance','upgrade':'ready','cold_restart':'ready'}))
finally:
    if client is not None:
        client.lifecycle('drain');client.lifecycle('shutdown')
'''
    result = subprocess.run([str(python), '-I', '-c', script, str(tmp_path / 'state')], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout + result.stderr)[-7000:]
    assert '"cold_start": "ready"' in result.stdout


def test_source_archive_contains_its_build_backend_and_builds_outside_checkout(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    env = {**os.environ, 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
    made = subprocess.run([sys.executable, '-c',
        'import sys;from tools.build_runtime import build_sdist;build_sdist(sys.argv[1])', str(dist)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
    assert made.returncode == 0, (made.stdout + made.stderr)[-5000:]
    archive = next(dist.glob('*.tar.gz'))
    with tarfile.open(archive) as source:
        names = {name.split('/', 1)[1] for name in source.getnames() if '/' in name}
    assert {'tools/build_runtime.py', 'tools/publish_runtime.py', 'codey/runtime.json',
            'frontend/src/desktop_splash.css', 'tests/test_desktop_startup.py'}.issubset(names)
    built = subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(archive), '--no-deps',
                            '--no-build-isolation', '--wheel-dir', str(dist)], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, (built.stdout + built.stderr)[-5000:]
