"""Scripted entry scenarios are offline and release stores after task errors."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


def test_scripted_ci_entry_cases_do_not_discover_a_real_local_endpoint(tmp_path):
    plugin = tmp_path / "offline_admission.py"
    plugin.write_text(
        'import pytest\n'
        '@pytest.fixture(autouse=True)\n'
        'def offline_local(monkeypatch):\n'
        '    from codey.providers import local_config, local_selection, local_discovery\n'
        '    for name in tuple(__import__("os").environ):\n'
        '        if name.startswith("LOCAL_OPENAI_"):\n'
        '            monkeypatch.delenv(name, raising=False)\n'
        '    empty = local_config.LocalProviderConfig(connection_revision="offline-fixture")\n'
        '    monkeypatch.setattr(local_config, "load_local_config", lambda: empty)\n'
        '    monkeypatch.setattr(local_selection, "load_local_config", lambda: empty)\n'
        '    def forbidden(*args, **kwargs):\n'
        '        raise AssertionError("scripted entry must not discover a real endpoint")\n'
        '    monkeypatch.setattr(local_discovery, "resolve_local_endpoint", forbidden)\n',
        encoding="utf-8",
    )
    targets = [
        "tests/test_gate_task_identity_excludes_global_status.py::GateTaskIdentityExcludesGlobalStatusTests::test_real_headless_done_with_status_row_passes_identity",
        "tests/test_required_modification_check_is_produced.py::test_real_headless_required_edit_and_fresh_verification_finish",
        "tests/test_web_execution_without_knowledge_store.py::test_real_headless_hybrid_executes_search_and_open_in_isolated_state",
        "tests/test_server.py::SessionThreadingTests::test_research_ui_path_reads_pdf_and_recovers_bad_excerpt",
    ]
    env = {**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "offline_admission", *targets, "--tb=short"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


def test_pdf_ui_fixture_closes_knowledge_store_when_task_submission_raises(tmp_path):
    from codey.knowledge.store import KnowledgeStore
    from tests import test_server

    stores = []
    closed = []
    original_close = KnowledgeStore.close

    def create_store(*args, **kwargs):
        store = KnowledgeStore(*args, **kwargs)
        stores.append(store)
        return store

    def close_store(store):
        closed.append(store)
        original_close(store)

    case = test_server.SessionThreadingTests()
    case.setUp()
    try:
        with (
            mock.patch.object(test_server.tempfile, "TemporaryDirectory", return_value=contextlib.nullcontext(str(tmp_path))),
            mock.patch("codey.knowledge.store.KnowledgeStore", side_effect=create_store),
            mock.patch.object(KnowledgeStore, "close", close_store),
            mock.patch.object(test_server.task_submit, "run_task", side_effect=RuntimeError("fixture submission failed")),
            pytest.raises(RuntimeError, match="fixture submission failed"),
        ):
            case.test_research_ui_path_reads_pdf_and_recovers_bad_excerpt()
        assert stores and all(store in closed for store in stores)
    finally:
        for store in stores:
            original_close(store)
        case.tearDown()
