from __future__ import annotations

from types import SimpleNamespace

from codey.launcher import releases


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
