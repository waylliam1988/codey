"""Recovery diagnostics use their written UTF-8 encoding on every locale."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from tools.local_model_gate_recovery import CONTENT, _verify_recovery


def test_unicode_recovery_receipts_are_read_with_explicit_utf8(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    checkpoint = project / "checkpoint.py"
    checkpoint.write_text(CONTENT, encoding="utf-8")
    policy = {"grants": ["control", "project.write", "project.verify"]}
    original = {"pid": 101, "result_text": "\u5df2\u521b\u5efa checkpoint.py", "policy": policy,
                "file_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}
    resumed = {"pid": 202, "policy": policy, "stop_reason": "done",
               "original_result_delivered": True, "original_call_id_preserved": True}
    for name, row in (("interruption.json", original), ("resume-result.json", resumed),
                      ("executions.jsonl", {"tool": "edit", "detail": "\u7f16\u8f91\u6210\u529f"}),
                      ("provider.jsonl", {"type": "request", "payload": {"messages": [{"content": original["result_text"]}]}})):
        (tmp_path / name).write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    read = Path.read_text

    def locale_read(path, encoding=None, errors=None):
        return read(path, encoding=encoding or "ascii", errors=errors)

    monkeypatch.setattr(Path, "read_text", locale_read)
    monkeypatch.setattr("tools.local_model_gate_recovery.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="Ran 1 test"))
    assert _verify_recovery(tmp_path, project, 75)["ok"] is True
