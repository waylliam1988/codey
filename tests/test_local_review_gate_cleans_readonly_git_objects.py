"""Windows read-only Git objects must not turn a successful smoke into failure."""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tools import local_model_gate_attempts as runner


def test_real_attempt_cleanup_handles_readonly_git_object(tmp_path, monkeypatch):
    unlink = os.unlink

    def windows_unlink(path, *args, **kwargs):
        if Path(path).name == "git_object" and not os.stat(path, dir_fd=kwargs.get("dir_fd")).st_mode & stat.S_IWUSR:
            raise PermissionError("Windows read-only file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", windows_unlink)
    roots = []

    def start(command, **kwargs):
        config = json.loads((Path(command[-1]) / "input.json").read_text(encoding="utf-8"))
        root = Path(config["project"])
        roots.extend((root, Path(config["state"])))
        obj = root / ".git" / "objects" / "git_object"
        obj.parent.mkdir(parents=True)
        obj.write_text("object", encoding="utf-8")
        obj.chmod(stat.S_IREAD)
        return Mock(), None

    monkeypatch.setattr(runner, "start_process", start)
    monkeypatch.setattr(runner, "wait_process", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(runner, "_child_result", lambda *_: {"case": "review", "ok": True})
    monkeypatch.setattr(runner, "_provider_metrics", lambda _: {"capture_incomplete": False})
    result = runner.run_case_process("review", tmp_path / "attempt", runner.GateTarget("http://localhost/v1", "test", 8000, 1000, 2000), timeout=5)
    assert result["ok"] is True, result
    assert all(not root.exists() for root in roots)
