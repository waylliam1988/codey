"""Paused repair drivers leave intent settlement to the outer lifecycle."""
from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from unittest import mock

import pytest

from codey.operations import project_completion_enforcement as enforcement
from codey.runtime.core.run_result import RunResult


@pytest.mark.parametrize("reason", ["approval", "stopped"])
def test_paused_repair_does_not_claim_driver_settlement(tmp_path, reason):
    repair_result = RunResult("paused", reason, 2)
    ctx = SimpleNamespace(
        result=RunResult("initial", "done", 1), request=SimpleNamespace(max_turns=8),
        proof=SimpleNamespace(satisfied=False, status="failed", to_payload=lambda: {}),
        completion_engine=object(), state=SimpleNamespace(run_registry=SimpleNamespace(stop_flag=Event())),
        decision=SimpleNamespace(failure_class="tests_failed", analysis_run_refs=()),
        selected_check=None, work=SimpleNamespace(evidence=object()), files=("pricing.py",), project=tmp_path,
        blocked_reason="", hooks=SimpleNamespace(on_event=lambda e: None),
        behavioral_plan=None, behavioral_observation=None,
        frame=SimpleNamespace(provider_id="local"), failover=SimpleNamespace(run=lambda **kw: repair_result),
    )
    transitions = []
    with (
        mock.patch.object(enforcement, "repair_candidate", return_value=True),
        mock.patch.object(enforcement, "project_repair_context", return_value=SimpleNamespace(
            admitted=True, to_payload=lambda: {"digest": "sha256:" + "0" * 64})),
        mock.patch.object(enforcement, "decisive_failure_fact", return_value={}),
        mock.patch.object(enforcement, "_refresh_checkpoint_view", return_value=object()),
        mock.patch.object(enforcement, "_commit_runtime_operation", side_effect=lambda ctx, name, fn: transitions.append(name)),
    ):
        enforcement._maybe_run_completion_repair(ctx)
    assert "mark_repair_running" in transitions
    assert "mark_repair_settled" not in transitions
    assert ctx.result.stop_reason == reason
    assert ctx.result.turns == 3
    assert ctx.result.checks_passed is False
