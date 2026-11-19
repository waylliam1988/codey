from __future__ import annotations

from types import SimpleNamespace

import pytest

from codey.control.protocol import ControlError
from codey.launcher import releases
from tools.publish_runtime import publish_runtime


def test_frontend_source_change_is_built_and_published(tmp_path, monkeypatch):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "publish_frontend.py").write_text("# publisher\n", encoding="utf-8")
    commands = []
    results = iter([SimpleNamespace(returncode=1, stdout="stale", stderr=""),
                    SimpleNamespace(returncode=0, stdout="built", stderr=""),
                    SimpleNamespace(returncode=0, stdout="published", stderr="")])

    def run(argv, **kwargs):
        commands.append(argv)
        return next(results)

    monkeypatch.setattr(releases.subprocess, "run", run)
    releases.ensure_frontend(tmp_path)
    assert commands == [
        [releases.sys.executable, "-m", "tools.publish_frontend", "--check"],
        ["npm", "run", "build"],
        [releases.sys.executable, "-m", "tools.publish_frontend"],
    ]


def test_changed_source_during_copy_does_not_publish_or_poison_the_release(tmp_path, monkeypatch):
    source = tmp_path / "checkout"
    (source / "codey/web").mkdir(parents=True)
    (source / "codey/__main__.py").write_text("pass\n", encoding="utf-8")
    page = source / "codey/web/index.html"
    page.write_text("original", encoding="utf-8")
    project = source / "pyproject.toml"
    project.write_text('[project]\nrequires-python = ">=3.11"\n', encoding="utf-8")
    publish_runtime(source, project, version="test")
    home = tmp_path / "state"
    digest = releases.runtime_digest(source)
    target = releases.release_path(home, "release_" + digest[:32]) / "source"
    copyfile = releases.shutil.copyfile

    def copy_and_edit(path, destination):
        copyfile(path, destination)
        if path.name == page.name:
            page.write_text("concurrent edit", encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setattr(releases.shutil, "copyfile", copy_and_edit)
        with pytest.raises(ControlError, match="Release changed") as error:
            releases.snapshot(home, source)
    assert error.value.code == "REVISION_CONFLICT"
    assert not target.exists()
    assert not list((home / "releases").glob(".snapshot-*"))

    page.write_text("original", encoding="utf-8")
    result = releases.snapshot(home, source)
    assert result["candidate_digest"] == releases.runtime_digest(target) == digest
