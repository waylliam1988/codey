from __future__ import annotations

import sys
from pathlib import Path

from tests.manual.kernel_unification_ab import (
    LEGACY_COMMIT,
    _codey_command,
    _source_root_for_arm,
)


def test_kernel_unification_ab_uses_requested_legacy_commit() -> None:
    assert LEGACY_COMMIT == "958bcb485bf05d0ae8232763681d1df5ecee1d34"


def test_kernel_unification_ab_resolves_distinct_source_roots(tmp_path: Path) -> None:
    unified = tmp_path / "unified"
    legacy = tmp_path / "legacy"
    assert _source_root_for_arm("codey_unified", unified, legacy) == unified.resolve()
    assert _source_root_for_arm("codey_pre_unified", unified, legacy) == legacy.resolve()


def test_kernel_unification_ab_runs_same_agent_command_for_both_arms(tmp_path: Path) -> None:
    command = _codey_command(tmp_path / "project", tmp_path / "state", 8)
    assert command[:7] == [
        sys.executable,
        "-m",
        "codey",
        "agent",
        "--json",
        "--provider",
        "local",
    ]
    assert command[-1].startswith("Fix app.py")


def test_kernel_comparison_configures_output_before_provider_admission(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.manual import kernel_unification_ab as ab
    captured = []
    monkeypatch.setattr(ab.subprocess, "run", lambda *a, **k: (
        captured.append(k["env"]) or SimpleNamespace(stdout="", stderr="", returncode=0)))
    monkeypatch.setattr(ab, "_run_verification", lambda *a, **k: {"passed": False})
    ab._run_arm("codey_unified", tmp_path, tmp_path, tmp_path / "artifacts", "http://127.0.0.1:1",
                model_id="fixture", max_turns=3, max_tokens=512, native_tools=True)
    assert captured[0]["LOCAL_OPENAI_CONTEXT_RESERVE"] == "512"


def test_kernel_comparison_keeps_zero_usage_and_marks_missing_exchanges_unknown(tmp_path, monkeypatch):
    import json

    from tests.manual import kernel_unification_ab as ab
    legacy = tmp_path / "legacy"
    (legacy / "codey").mkdir(parents=True)
    directory = tmp_path / "result"
    monkeypatch.setattr(sys, "argv", ["ab", "--run-dir", str(directory), "--legacy-root", str(legacy)])
    class Proxy:
        server_port = 1
        records = [
            {"arm": "codey_pre_unified", "path": "/v1/chat/completions", "response": {"usage": {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}},
            {"arm": "codey_unified", "path": "/v1/chat/completions", "response": {"usage": {
                "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}},
            {"arm": "codey_unified", "path": "/v1/chat/completions", "response": {}}]
        def __init__(self, *a, **k): pass
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): pass
    monkeypatch.setattr(ab, "_Proxy", Proxy)
    monkeypatch.setattr(ab, "_run_arm", lambda *a, **k: {"status": "completed"})
    ab.main()
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert result["arms"]["codey_pre_unified"]["token_usage"] == 0
    assert result["arms"]["codey_unified"]["token_usage"] is None
    assert result["arms"]["codey_unified"]["known_token_usage"] == 5
