"""Browser durations belong to individual settled effects and survive projection."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from codey.app.event_payloads import browser_event_payload, machine_event_payload
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel_events import _emit_tool_results
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.types.json_value import decode_json_object


class Clock:
    def __init__(self) -> None:
        self.values: Iterator[float] = iter((10.0, 10.025, 500.0, 500.004))

    def monotonic(self) -> float:
        return next(self.values)


def test_browser_effect_duration_excludes_worker_uptime_and_replay_does_not_retime_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("codey.operations.kernel.kernel_execution.time", Clock())
    session = TaskSession(TaskPolicy(frozenset({"control", "browser.interact"})))
    calls = [ToolCall("browser_click", {"ref": "observed"}, "first"),
             ToolCall("browser_click", {"ref": "observed"}, "second")]

    def execute(call: ToolCall) -> ToolResult:
        return ToolResult(call, "Delivered", ok=True, canonical={"value": {"delivery": "delivered"}})

    results = [execute_turn(session, [call], executors={"browser_click": execute}, run_id="r", turn=turn)[0]
               for turn, call in enumerate(calls, 1)]
    times = [decode_json_object(result.audit["browser_execution"])["elapsed_ms"] for result in results]
    assert times[0] in {24, 25} and times[1] in {3, 4}
    restored = execute_turn(session, [calls[0]], executors={"browser_click": execute}, run_id="r", turn=1)[0]
    assert restored.audit["browser_execution"] == results[0].audit["browser_execution"]
    for turn, result in enumerate(results, 1):
        events: list[RunEvent] = []
        _emit_tool_results(events.append, session, [result], run_id="r", turn=turn)
        payload = run_event_ui_payload("r", "s", events[0])
        assert payload is not None
        ui, headless = browser_event_payload(payload), machine_event_payload(payload)
        assert ui is not None and headless is not None
        for key in ("elapsed_ms", "effect_ref", "phase"):
            assert ui[key] == headless[key] == payload[key]
        assert ui["elapsed_ms"] == times[turn - 1] and ui["phase"] == "finished"
