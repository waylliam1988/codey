"""Admitted model/protocol survive process restart without persisting credentials."""
import json
from dataclasses import replace

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.providers import local_config
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.write.task_runtime import TaskRuntime
from codey.task.model import TaskSubmission


@pytest.mark.parametrize("thinking,effort", [(False, None), (True, "low")])
def test_default_local_admission_keeps_saved_controls_without_leaking_to_other_models(tmp_path, monkeypatch, thinking, effort):
    from codey.providers.local_connection import capture_selection, open_selection

    for name in ("LOCAL_OPENAI_BASE_URL", "LOCAL_OPENAI_MODEL", "LOCAL_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(local_config, "DEFAULT_STATE_HOME", tmp_path)
    monkeypatch.setattr("codey.providers.local_tokens.admitted_counter", lambda *args: ("estimated", 32768))
    config = local_config.LocalProviderConfig(base_url="http://localhost:9/v1", model="original", connection_revision="test",
        thinking_enabled=thinking, reasoning_effort=effort)
    local_config.save_local_config(config)
    frozen = capture_selection()
    assert frozen.thinking_enabled is thinking and frozen.reasoning_effort == effort
    local_config.save_local_config(replace(config, thinking_enabled=not thinking, reasoning_effort=None))
    provider = open_selection(frozen)
    assert provider.thinking_enabled is thinking and provider.reasoning_effort == effort
    explicit = capture_selection({"base_url": config.base_url, "model": config.model})
    assert explicit.thinking_enabled is None and explicit.reasoning_effort is None
    monkeypatch.setenv("LOCAL_OPENAI_MODEL", "different-model")
    other = capture_selection()
    assert other.model_id == "different-model"
    assert other.thinking_enabled is None and other.reasoning_effort is None


def test_cold_start_keeps_admitted_selection_and_does_not_write_secret(tmp_path):
    selection = ApiRunSelection("local", "rev_original", "model_original", "openai-responses", True)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    durable.admit_input(
        conversation_id="session", request_id="request-run", task_id="run", text="hello",
        provider_snapshot={"provider_id": "local", "model_id": "model_original"},
        task_state={"model_selection": selection.to_payload()},
    )
    request = TaskSubmission("session", None, "hello", 2, False, "local", run_id="run", model_selection=selection.to_payload())
    TaskRuntime(lambda request: OperationOutcome.suspended(reason="approval"), durable=durable).run(request)
    durable.close()
    reloaded_durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    reloaded = RuntimeOperationStore(durable=reloaded_durable).load("session", "run")
    assert ApiRunSelection.from_payload(reloaded.model_selection) == selection
    assert "api_key" not in str(reloaded_durable.entries("session"))
    reloaded_durable.close()


def test_changed_connection_blocks_restore_but_changed_model_keeps_original(tmp_path, monkeypatch):
    from codey.providers.local_connection import capture_selection, config_for_selection

    monkeypatch.setattr(local_config, "DEFAULT_STATE_HOME", tmp_path)
    original = local_config.LocalProviderConfig(base_url="http://localhost:9/v1", model="original", api_key="secret-fixture", connection_revision="original-revision")
    local_config.save_local_config(original)
    selection = capture_selection({"base_url": original.base_url, "model": "original"})
    local_config.save_local_config(replace(original, model="new-model", api_protocol="openai-responses"))
    restored = config_for_selection(selection)
    assert restored.model == "original" and restored.api_protocol == "openai-completions"
    assert "secret-fixture" not in json.dumps(selection.to_payload())
    local_config.save_local_config(replace(original, base_url="http://localhost:10/v1", connection_revision="replacement"))
    import pytest

    with pytest.raises(ValueError, match="connection"):
        config_for_selection(selection)


def test_shell_approval_keeps_zen_selection_without_local_specific_branch(tmp_path):
    from codey.app.context import AppContext
    from tests.support.shell_approval import add_pending_approval, admit_shell_task
    from tests.support.workspace_conversation import short_test_state_home

    state = AppContext(short_test_state_home())
    try:
        run = state.reserve_run(session_id="session", project=None, task="test", provider_id="zen")
        selection = ApiRunSelection("zen", "revision", "model", "openai-responses", True)
        state.run_registry.bind_api_selection(run.run_id, selection)
        admit_shell_task(state, session_id="session", run_id=run.run_id, task="test", provider_id="zen")
        add_pending_approval(state, "approval", {"run_id": run.run_id, "provider": "zen", "session_id": "session", "command": "echo approval", "cwd": "."})
        assert state.pending_shell_approvals()["approval"]["_api_selection"] == selection
    finally:
        state.close()


def test_production_admission_restores_original_selection_after_settings_and_restart(tmp_path, monkeypatch):
    from codey.app.context import AppContext
    from codey.operations.task_entry import _admit_api_selection

    monkeypatch.setattr(local_config, "DEFAULT_STATE_HOME", tmp_path / "settings")
    config = local_config.LocalProviderConfig(base_url="http://localhost:9/v1", model="first", api_protocol="openai-responses", connection_revision="scope")
    local_config.save_local_config(config)
    state_home = tmp_path / "state"
    state = AppContext(state_home)
    try:
        admitted = _admit_api_selection(state, TaskSubmission("s", None, "task", 4, False, "local", run_id="first-run"))
        from codey.providers.snapshot import provider_snapshot

        state.durable.admit_input(
            conversation_id="s",
            request_id="first-run",
            task_id="first-run",
            text="task",
            provider_snapshot=provider_snapshot(
                "local",
                ApiRunSelection.from_payload(admitted.model_selection),
            ),
            task_state={"task": "task", "provider_id": "local", "model_selection": admitted.model_selection},
        )
        TaskRuntime(lambda _: OperationOutcome.suspended(reason="approval"), durable=state.durable).run(admitted)
    finally:
        state.close()
    local_config.save_local_config(replace(config, model="second", api_protocol="openai-completions"))
    restarted = AppContext(state_home)
    try:
        restored = _admit_api_selection(restarted, TaskSubmission("s", None, "resume", 4, True, "local", run_id="next-run", previous_run_id="first-run"))
        frozen = ApiRunSelection.from_payload(restored.model_selection)
        assert frozen.model_id == "first"
        assert frozen.protocol == "openai-responses"
        assert restored.model_selection == admitted.model_selection
    finally:
        restarted.close()
