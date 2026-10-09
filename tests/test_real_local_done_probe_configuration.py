"""The done probe configures its output limit before the provider counts."""

import sys
from types import SimpleNamespace

from tests.manual import real_local_done_probe as probe


def test_probe_output_limit_reaches_provider_configuration(tmp_path, monkeypatch):
    directory = tmp_path / "artifacts"
    monkeypatch.setattr(sys, "argv", ["probe", "--run-dir", str(directory), "--max-tokens", "512"])
    monkeypatch.setattr(probe.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    class Proxy:
        records = [{"response": {"choices": [{"message": {"tool_calls": [{"function": {"name": "done"}}]}}]}}]
        def __init__(self, *args, **kwargs): pass
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): pass
    monkeypatch.setattr(probe, "_Proxy", Proxy)
    environments = []
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: (
        environments.append(k["env"]) or SimpleNamespace(stdout="", stderr="", returncode=0)))
    assert probe.main() == 0
    assert environments[0]["LOCAL_OPENAI_CONTEXT_RESERVE"] == "512"
