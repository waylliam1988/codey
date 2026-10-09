"""Real Local preferences and identical writer/reviewer sampling in the worker."""

import sys
from types import SimpleNamespace

import pytest

from tests.manual import agent_stability_codey_worker as worker


def test_experiment_context_enables_only_the_actual_local_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEY_AB_TRACE", str(tmp_path / "trace"))
    monkeypatch.setenv("LOCAL_OPENAI_MODEL", "actual-model")
    ctx = worker.ControlledContext(tmp_path / "state", port=9222, emit_jsonl=lambda row: None)
    try:
        sources = ctx.providers.model_preferences.snapshot()["sources"]
        assert sources["local"] == {"enabled": True, "models": ["actual-model"]}
        assert all(not source["enabled"] for key, source in sources.items() if key != "local")
    finally:
        ctx.close()


def test_writer_and_reviewer_use_the_same_pre_admission_sampling_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["worker", "--project", str(tmp_path), "--state-home", str(tmp_path / "state"),
                                     "--session-id", "fixture", "task"])
    monkeypatch.setattr(worker.headless_runner, "HeadlessAppContext", worker.headless_runner.HeadlessAppContext)
    calls = []
    monkeypatch.setattr(worker.headless_runner, "run_headless", lambda *a, **k: (
        calls.append(k) or SimpleNamespace(exit_code=0)))
    assert worker.main() == 0
    assert calls[0]["connect_provider"] is worker.sampling_provider
    assert calls[0]["connect_reviewer"] is worker.sampling_provider


def test_sampling_factory_refuses_nonlocal_providers_before_connecting(monkeypatch):
    attempted = []
    monkeypatch.setenv("CODEY_AB_TEMPERATURE", "0")
    monkeypatch.setattr(worker, "connect_provider", lambda *a, **k: (
        attempted.append(a) or SimpleNamespace(configure_request=lambda payload: None)))
    with pytest.raises(RuntimeError, match="only Local"):
        worker.sampling_provider("zen")
    assert attempted == []
