"""One run owns callback state; failover configuration never expands on failure."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.operations.context import RunWork
from codey.operations.provider_preflight import connect_provider_with_preflight
from codey.operations.task_phases.hooks import build_hooks
from codey.providers.diagnostics import ProviderFailure
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.execution_evidence import ExecutionEvidence


def make_hooks(*, order=lambda: (), work=None, state=None):
    state = state or SimpleNamespace(
        providers=SimpleNamespace(supervisor=None),
        self_repair=None,
        emit=Mock(),
        provider_failover_order=order,
        add_pending_shell_approval=Mock(),
    )
    work = work or RunWork(recent_events=[], evidence=ExecutionEvidence())
    deps = SimpleNamespace(work_checkpoints=None, workspace_revisions=None)
    hooks = build_hooks(
        deps, state, work, session_id="s", run_id="r", project=None,
        max_turns=3, project_config_ignored=(), review_log_lines=2,
        project_completion_deps=Mock(),
    )
    return hooks, state, work, deps


def test_failure_is_deduplicated_per_run_not_across_runs():
    failure = ProviderFailure("local", "send", "", "", "offline", "now")
    first_work = RunWork(recent_events=[], evidence=ExecutionEvidence(), ledger=Mock())
    second_work = RunWork(recent_events=[], evidence=ExecutionEvidence(), ledger=Mock())
    first, _, _, _ = make_hooks(work=first_work)
    second, _, _, _ = make_hooks(work=second_work)
    first.record_provider_failure("local", failure)
    first.record_provider_failure("local", failure)
    second.record_provider_failure("local", failure)
    first_work.ledger.append_provider_failure.assert_called_once_with("local", failure)
    second_work.ledger.append_provider_failure.assert_called_once_with("local", failure)


def test_event_fanout_records_turn_before_tool_start_returns():
    hooks, state, work, _ = make_hooks()
    work.ledger = Mock()
    work.record_agent_events_in_ledger = True
    work.evidence = Mock()
    event = RunEvent.tool_started(3, ToolCall(name="read_file", args={"path": "a.py"}), "Reading")
    hooks.on_event(event)
    assert work.turns_observed == 3
    work.ledger.append_run_event.assert_called_once_with(event)
    state.emit.assert_called_once()
    work.evidence.record.assert_not_called()
    assert work.recent_events == []


def test_checkpoint_callback_uses_latest_store_and_checkpoint():
    hooks, _, work, deps = make_hooks()
    action = Mock(return_value="second")
    hooks.update_checkpoint(action)
    action.assert_not_called()
    deps.work_checkpoints = object()
    work.work_checkpoint = "first"
    hooks.update_checkpoint(action)
    action.assert_called_once_with(deps.work_checkpoints, "first")
    assert work.work_checkpoint == "second"
    action.side_effect = OSError("disk offline")
    hooks.update_checkpoint(action)
    assert work.work_checkpoint == "second"
    action.side_effect = TypeError("programming error")
    with pytest.raises(TypeError, match="programming error"):
        hooks.update_checkpoint(action)


def test_failover_order_is_lazy_and_keeps_configured_empty_order():
    current = []
    loader = Mock(side_effect=lambda: tuple(current))
    hooks, _, _, _ = make_hooks(order=loader)
    loader.assert_not_called()
    assert hooks.provider_failover_order() == ()
    current[:] = ["local", "qwen"]
    assert hooks.provider_failover_order() == ("local", "qwen")


def test_failover_configuration_error_surfaces_without_catalog_fallback():
    hooks, _, _, _ = make_hooks(order=Mock(side_effect=OSError("config unavailable")))
    with pytest.raises(OSError, match="config unavailable"):
        hooks.provider_failover_order()


def test_missing_failover_contract_surfaces_without_catalog_fallback():
    hooks, state, _, _ = make_hooks()
    del state.provider_failover_order
    with pytest.raises(AttributeError, match="provider_failover_order"):
        hooks.provider_failover_order()


def test_preflight_does_not_switch_provider_when_order_cannot_be_loaded():
    hooks, state, _, _ = make_hooks(order=Mock(side_effect=OSError("config unavailable")))
    state.get_provider = Mock(side_effect=OSError("connection refused"))
    state.switch_run_provider = Mock()
    supervisor = Mock()
    supervisor.is_available.return_value = True
    failure = ProviderFailure("local", "connect", "", "", "offline", "now")
    with pytest.raises(OSError, match="config unavailable"):
        connect_provider_with_preflight(
            state=state, run_id="r", provider_id="local", supervisor=supervisor,
            ranked_failover_order=hooks.provider_failover_order,
            capture_provider_failure=Mock(return_value=failure),
            record_provider_failure=hooks.record_provider_failure,
            append_ledger=hooks.append_ledger, trace_sink=Mock(),
        )
    state.get_provider.assert_called_once_with("local")
    supervisor.select.assert_not_called()
    state.switch_run_provider.assert_not_called()
