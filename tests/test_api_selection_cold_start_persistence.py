"""Admitted model/protocol survive process restart without persisting credentials."""
import json
from dataclasses import replace

from codey.providers import local_config
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.task_runtime import TaskRuntime
from codey.task.model import TaskSubmission


def test_cold_start_keeps_admitted_selection_and_does_not_write_secret(tmp_path):
    selection = ApiRunSelection("local", "rev_original", "model_original", "openai-responses", True)
    log = RuntimeSessionLog(tmp_path)
    request = TaskSubmission("session", None, "hello", 2, False, "local", run_id="run", model_selection=selection.to_payload())
    TaskRuntime(log, lambda request: OperationOutcome.suspended(reason="approval")).run(request)
    reloaded = RuntimeOperationStore(RuntimeSessionLog(tmp_path)).load("session", "run")
    assert ApiRunSelection.from_payload(reloaded.model_selection) == selection
    assert "api_key" not in log.path_for("session").read_text(encoding="utf8")


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

    state = AppContext(tmp_path)
    try:
        run = state.reserve_run(session_id="session", project=None, task="test", provider_id="zen")
        selection = ApiRunSelection("zen", "revision", "model", "openai-responses", True)
        state.run_registry.bind_api_selection(run.run_id, selection)
        state.add_pending_shell_approval("approval", {"run_id": run.run_id, "provider": "zen", "session_id": "session"})
        assert state.pop_pending_shell_approval("approval")["_api_selection"] == selection
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
        TaskRuntime(state.runtime_log, lambda _: OperationOutcome.suspended(reason="approval")).run(admitted)
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
