from __future__ import annotations

import unittest
from unittest import mock

from codey.operations.provider_preflight import (
    ProviderSwitchDenied,
    connect_provider_with_preflight,
)
from codey.providers.diagnostics import ProviderActionError, ProviderFailure


class _Provider:
    pass


class _CloseFailingProvider:
    def close(self) -> None:
        raise RuntimeError("close failed")


class _State:
    def __init__(self) -> None:
        self.switched: list[tuple[str, str]] = []

    def get_provider(self, _provider_id: str) -> _Provider:
        return _Provider()

    def switch_run_provider(self, run_id: str, provider_id: str) -> None:
        self.switched.append((run_id, provider_id))


class _FailingState(_State):
    def get_provider(self, _provider_id: str) -> _Provider:
        raise RuntimeError("connect failed")


class _CanaryState(_State):
    def __init__(self) -> None:
        super().__init__()
        self.deepseek = _CloseFailingProvider()
        self.qwen = _Provider()

    def get_provider(self, provider_id: str) -> object:
        return self.deepseek if provider_id == "deepseek" else self.qwen


class _SameProviderSupervisor:
    def prepare_user_selected(self, provider_id: str) -> str:
        return provider_id

    def is_available(self, _provider_id: str) -> bool:
        return False

    def select(self, *_args, **_kwargs) -> str:
        return "deepseek"

    def needs_canary(self, _provider_id: str) -> bool:
        return False


class _CanarySupervisor:
    def prepare_user_selected(self, provider_id: str) -> str:
        return provider_id

    def is_available(self, _provider_id: str) -> bool:
        return True

    def select(self, *_args, **_kwargs) -> str:
        return "qwen"

    def needs_canary(self, _provider_id: str) -> bool:
        return True


class _Trace:
    def __init__(self) -> None:
        self.decisions: list[object] = []
        self.fallbacks: list[dict[str, object]] = []

    def call(self, method: str, *args: object, **kwargs: object) -> None:
        fn = getattr(self, method)
        fn(*args, **kwargs)

    def record_policy_decision(self, decision: object) -> None:
        self.decisions.append(decision)

    def record_fallback(self, **kwargs: object) -> None:
        self.fallbacks.append(dict(kwargs))


class _Ledger:
    def __init__(self, rows: list[tuple[str, dict[str, object]]]) -> None:
        self.rows = rows

    def append(self, event_type: str, **fields: object) -> None:
        self.rows.append((event_type, fields))


class ProviderPreflightTests(unittest.TestCase):
    def test_same_provider_fallback_is_denied_before_state_switch(self) -> None:
        state = _State()
        trace = _Trace()
        rows: list[tuple[str, dict[str, object]]] = []

        with self.assertRaises(ProviderSwitchDenied):
            connect_provider_with_preflight(
                state=state,
                run_id="run-1",
                provider_id="deepseek",
                supervisor=_SameProviderSupervisor(),
                ranked_failover_order=lambda: ("deepseek", "qwen"),
                capture_provider_failure=lambda **_kwargs: None,
                record_provider_failure=lambda _provider_id, _failure: None,
                append_ledger=lambda fn: fn(_Ledger(rows)),
                trace_sink=trace,
            )

        self.assertEqual(state.switched, [])
        self.assertEqual(rows, [])
        self.assertEqual(len(trace.decisions), 1)
        decision = trace.decisions[0]
        self.assertEqual(getattr(decision, "decision", ""), "deny")
        self.assertEqual(getattr(decision, "reason_code", ""), "same_provider")

    def test_connect_failure_without_supervisor_does_not_auto_switch(self) -> None:
        state = _FailingState()
        trace = _Trace()
        rows: list[tuple[str, dict[str, object]]] = []
        failures: list[tuple[str, ProviderFailure]] = []

        with self.assertRaises(ProviderActionError):
            connect_provider_with_preflight(
                state=state,
                run_id="run-1",
                provider_id="deepseek",
                supervisor=None,
                ranked_failover_order=lambda: ("deepseek", "qwen"),
                capture_provider_failure=lambda **_kwargs: ProviderFailure(
                    model="DeepSeek",
                    action="connect",
                    url="",
                    title="",
                    message="connect failed",
                    time="2026-01-01T00:00:00+00:00",
                ),
                record_provider_failure=lambda provider_id, failure: failures.append(
                    (provider_id, failure)
                ),
                append_ledger=lambda fn: fn(_Ledger(rows)),
                trace_sink=trace,
            )

        self.assertEqual(state.switched, [])
        self.assertEqual(rows, [])
        self.assertEqual(trace.decisions, [])
        self.assertEqual([provider_id for provider_id, _failure in failures], ["deepseek"])

    def test_canary_close_failure_is_recorded_without_blocking_failover(self) -> None:
        state = _CanaryState()
        trace = _Trace()
        rows: list[tuple[str, dict[str, object]]] = []
        failures: list[tuple[str, ProviderFailure]] = []

        def capture(**kwargs: object) -> ProviderFailure:
            return ProviderFailure(
                model=str(kwargs["model"]),
                action=str(kwargs["action"]),
                url="",
                title="",
                message=str(kwargs["error"]),
                time="2026-01-01T00:00:00+00:00",
            )

        with mock.patch(
            "codey.operations.provider_preflight.run_half_open_canary",
            side_effect=[False, True],
        ):
            result = connect_provider_with_preflight(
                state=state,
                run_id="run-1",
                provider_id="deepseek",
                supervisor=_CanarySupervisor(),
                ranked_failover_order=lambda: ("deepseek", "qwen"),
                capture_provider_failure=capture,
                record_provider_failure=lambda provider_id, failure: failures.append(
                    (provider_id, failure)
                ),
                append_ledger=lambda fn: fn(_Ledger(rows)),
                trace_sink=trace,
            )

        self.assertEqual(result.provider_id, "qwen")
        self.assertEqual(state.switched, [("run-1", "qwen")])
        self.assertEqual([event for event, _fields in rows], ["provider_switched"])
        self.assertEqual(trace.fallbacks[0]["to_provider"], "qwen")
        self.assertEqual([(pid, failure.action) for pid, failure in failures], [("deepseek", "close")])


if __name__ == "__main__":
    unittest.main()
