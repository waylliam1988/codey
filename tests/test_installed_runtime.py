"""Build and install a real wheel, then launch outside the checkout."""
import os
import sys
import tarfile
from pathlib import Path

from codey.runtime.core import cancellation

ROOT = Path(__file__).resolve().parents[1]


def _test_environment():
    # Service/validation children do not inherit the probe's -I flag.
    env = {name: value for name, value in os.environ.items() if name not in {'PYTHONPATH', 'PYTHONHOME'}}
    env.update(PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1', PYTHONNOUSERSITE='1')
    return env


def _run_command(args, *, cwd, env, timeout):
    process, owner = cancellation.start_process(args, cwd=cwd, env=env)
    try:
        result = cancellation.wait_process(process, owner, args, timeout, capture_limit_bytes=256 * 1024)
    finally:
        # A successful parent can also leave service/backend descendants. In
        # particular, POSIX groups must be cleaned after the parent has exited.
        cancellation.terminate_process_tree(process, owner)
        if owner is not None:
            owner.close()
    assert result.returncode == 0, (result.stdout + result.stderr)[-7000:]
    return result


def test_wheel_cold_start_reconnect_and_upgrade_without_repository(tmp_path):
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    env = _test_environment()
    _run_command([sys.executable, '-m', 'pip', 'wheel', str(ROOT), '--no-deps', '--no-build-isolation',
                  '--wheel-dir', str(wheels)], cwd=tmp_path, env=env, timeout=180)
    environment = tmp_path / 'venv'
    # Dependencies are intentionally borrowed; creating the venv/bootstrapping
    # pip still needs a deadline and the same process-tree ownership as builds.
    _run_command([sys.executable, '-I', '-m', 'venv', '--system-site-packages', str(environment)],
                 cwd=tmp_path, env=env, timeout=90)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    _run_command([str(python), '-I', '-m', 'pip', 'install', '--no-deps', '--no-index', '--ignore-installed',
                  str(next(wheels.glob('*.whl')))], cwd=tmp_path, env=env, timeout=60)
    script = r'''
import json
import sys
import time
from pathlib import Path

import codey
from codey.control.client import ControlClient, ensure_service
from codey.launcher.runtime import read_runtime
from codey.research.search_factory import default_research_search_provider
from codey.research.web_sources import WebSourceProvider
from codey.storage.file_lock import acquire_lease

root=Path(codey.__file__).resolve().parent.parent
assert root.is_relative_to(Path(sys.prefix)), root
assert not (root/'pyproject.toml').exists()
assert not (root/'tools/publish_runtime.py').exists()
assert read_runtime(root)['version']==codey.__version__
assert not (root/'codey/research/chat_web.py').exists()
provider=default_research_search_provider()
assert isinstance(provider.base_provider,WebSourceProvider)
assert provider.base_provider._browser is None
provider.close()
home=Path(sys.argv[1])
client=ControlClient(home,operator=True)


def stop_service():
    discovery=home/'service.json'
    if not discovery.exists():
        return
    client.lifecycle('drain')
    deadline=time.monotonic()+15
    while not client.lifecycle('status')['quiescent']:
        assert time.monotonic()<deadline, 'Service did not become quiescent'
        time.sleep(0.05)
    client.lifecycle('shutdown')
    deadline=time.monotonic()+15
    while discovery.exists() and time.monotonic()<deadline:
        time.sleep(0.05)
    assert not discovery.exists(), 'Service did not remove its discovery registration'
    # Discovery removal precedes releasing ownership; wait for that too before
    # allowing a new instance to acquire the same state home.
    with acquire_lease(home/'.server.lock',timeout_seconds=5):
        pass


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
    stop_service()
    third=ensure_service(home,port=0).call('get_health')
    assert third['status']=='ready' and third['release_id']==second['release_id']
    assert third['instance_id']!=second['instance_id']
    stop_service()
    print(json.dumps({'cold_start':'ready','reconnect':'same instance','upgrade':'ready','cold_restart':'ready'}))
finally:
    failure=sys.exc_info()[1]
    try:
        stop_service()
    except Exception as cleanup_error:
        if failure is None:
            raise
        failure.add_note(f'Service cleanup also failed: {cleanup_error!r}')
'''
    result = _run_command([str(python), '-I', '-c', script, str(tmp_path / 'state')],
                          cwd=tmp_path, env=env, timeout=180)
    assert '"cold_start": "ready"' in result.stdout


def test_source_archive_contains_its_build_backend_and_builds_outside_checkout(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    env = _test_environment()
    _run_command([sys.executable, '-c',
        'import sys;from tools.build_runtime import build_sdist;build_sdist(sys.argv[1])', str(dist)],
        cwd=ROOT, env=env, timeout=90)
    archive = next(dist.glob('*.tar.gz'))
    with tarfile.open(archive) as source:
        names = {name.split('/', 1)[1] for name in source.getnames() if '/' in name}
    assert {'tools/build_runtime.py', 'tools/publish_runtime.py', 'codey/runtime.json',
            'frontend/src/desktop_splash.css', 'tests/test_desktop_startup.py'}.issubset(names)
    _run_command([sys.executable, '-m', 'pip', 'wheel', str(archive), '--no-deps',
                  '--no-build-isolation', '--wheel-dir', str(dist)], cwd=tmp_path, env=env, timeout=180)
