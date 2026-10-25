"""Source and installed artifacts share a single immutable runtime contract."""
import json
import os
import tomllib
from pathlib import Path

import pytest

from codey.control.protocol import ControlError
from codey.launcher.releases import runtime_digest, snapshot
from codey.launcher.runtime import dependencies, read_runtime
from tools.publish_runtime import publish_runtime


def test_package_discovery_never_enters_unrelated_workspace_trees(tmp_path, monkeypatch):
    from setuptools.config.expand import find_packages

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    discovery = config['tool']['setuptools']['packages']['find']
    for name in ('codey', 'codey/nested', 'tests', 'tools'):
        package = tmp_path / name
        package.mkdir(parents=True, exist_ok=True)
        (package / '__init__.py').touch()
    unrelated = tmp_path / 'reference-projects'
    (unrelated / 'node_modules' / 'deep').mkdir(parents=True)
    scandir = os.scandir

    def checked_scandir(path):
        assert not Path(path).is_relative_to(unrelated), 'package discovery entered a reference project'
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'scandir', checked_scandir)
        assert set(find_packages(root_dir=str(tmp_path), **discovery)) == {'codey', 'codey.nested'}
    # Disabling implicit namespaces must keep every actual Python module's package.
    actual = set(find_packages(root_dir=str(root), **discovery))
    for module in (root / 'codey').rglob('*.py'):
        package = module.parent.relative_to(root).as_posix().replace('/', '.')
        assert package in actual, module


@pytest.fixture
def published(tmp_path):
    source = tmp_path / 'source'
    (source / 'codey/web').mkdir(parents=True)
    (source / 'codey/__main__.py').write_text('print("Codey")')
    (source / 'codey/web/index.html').write_text('built')
    project = source / 'pyproject.toml'
    project.write_text('[project]\nversion="1.0"\nrequires-python=">=3.11"\ndependencies=["some-package>=1"]\n')
    publish_runtime(source, project)
    return source


def test_published_snapshot_requires_neither_checkout_nor_pyproject(published, tmp_path):
    before = runtime_digest(published)
    (published / 'pyproject.toml').unlink()
    result = snapshot(tmp_path / 'state', published)
    assert runtime_digest(Path(result['source'])) == before
    assert read_runtime(Path(result['source']))['version'] == '1.0'
    assert dependencies(Path(result['source'])) == ['>=3.11', ['some-package>=1'], []]
    assert not (Path(result['source']) / 'pyproject.toml').exists()


@pytest.mark.parametrize('damage', ['missing-file', 'extra-file', 'traversal', 'corrupt', 'missing-manifest'])
def test_incomplete_or_unpublished_runtime_is_rejected(published, damage):
    descriptor = published / 'codey/runtime.json'
    if damage == 'missing-file':
        (published / 'codey/web/index.html').unlink()
    elif damage == 'extra-file':
        (published / 'codey/unpublished.py').write_text('unexpected')
    elif damage == 'traversal':
        data = json.loads(descriptor.read_text())
        data['files'].append('../outside.py')
        descriptor.write_text(json.dumps(data))
    elif damage == 'corrupt':
        descriptor.write_text('not JSON')
    else:
        descriptor.unlink()
    with pytest.raises(ControlError, match='runtime is missing or incomplete'):
        runtime_digest(published)


def test_old_repository_metadata_does_not_substitute_for_a_published_runtime(published):
    (published / 'codey/runtime.json').unlink()
    with pytest.raises(ControlError, match='runtime is missing or incomplete'):
        dependencies(published)


def test_python_caches_do_not_change_the_published_runtime(published):
    before = runtime_digest(published)
    (published / 'codey/__pycache__').mkdir()
    (published / 'codey/__pycache__/compiled.pyc').write_bytes(b'cache')
    assert runtime_digest(published) == before
