"""A user stop must prevent another Writer attempt or provider takeover."""

from collections.abc import Callable

import pytest

from codey.agents.writer_failover import CheckpointView, WriterAttempt
from codey.providers.base import ChatProvider
from codey.providers.transport import ProviderTransport
from codey.runtime.core.cancellation import TaskCancelled
from codey.runtime.core.run_result import RunResult
from tests.test_writer_failover import _fail, _Harness, _Provider, _succeed


@pytest.mark.parametrize("connected", [False, True])
def test_a_stopped_task_starts_no_writer_or_connection(connected: bool) -> None:
    harness = _Harness(order=["p1", "p2"], stopped=True)
    harness.script = [_succeed()]
    runner = harness.make_runner(provider=_Provider("p1") if connected else None, provider_id="p1")

    with pytest.raises(TaskCancelled):
        runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())

    assert not harness.log and not harness.specs and not harness.switched


def test_stop_during_failed_initial_connection_prevents_takeover() -> None:
    class Harness(_Harness):
        def connect(self, pid: str) -> ProviderTransport:
            if pid == "p1":
                self._stopped = True
            return super().connect(pid)

    harness = Harness(order=["p1", "p2"], connect_fail={"p1"})
    harness.script = [_succeed()]
    runner = harness.make_runner(provider=None, provider_id="p1")

    with pytest.raises(TaskCancelled):
        runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())

    assert harness.log == [("connect", "p1")]
    assert not harness.switched and not harness.specs


def test_stop_during_failed_writer_attempt_prevents_takeover() -> None:
    harness = _Harness(order=["p1", "p2"])

    def stop_then_fail(spec: WriterAttempt, note: Callable[[int], None]) -> RunResult:
        harness._stopped = True
        return _fail()(spec, note)

    harness.script = [stop_then_fail]
    runner = harness.make_runner(provider=_Provider("p1"), provider_id="p1")

    with pytest.raises(TaskCancelled):
        runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())

    assert len(harness.specs) == 1
    assert not harness.log and not harness.switched


@pytest.mark.parametrize("stage", ["failed_attempt", "canary"])
@pytest.mark.parametrize("cleanup_error", [TaskCancelled, RuntimeError])
def test_cleanup_propagates_stop_but_keeps_ordinary_failures_best_effort(
    stage: str, cleanup_error: type[Exception],
) -> None:
    class Harness(_Harness):
        def close(self, provider: ChatProvider | ProviderTransport) -> None:
            super().close(provider)
            raise cleanup_error("cleanup")

    harness = Harness(
        order=["p1", "p2", "p3"], connect_fail={"p1"} if stage == "canary" else (),
        needs_canary={"p2"}, canary_fail={"p2"} if stage == "canary" else (),
    )
    harness.script = [_succeed()] if stage == "canary" else [_fail(), _succeed()]
    runner = harness.make_runner(provider=None if stage == "canary" else _Provider("p1"), provider_id="p1")

    if cleanup_error is TaskCancelled:
        with pytest.raises(TaskCancelled, match="cleanup"):
            runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())
        assert runner.provider is None
        assert harness.switched == (["p2"] if stage == "canary" else [])
    else:
        result = runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())
        assert result.summary == "ok"
        assert runner.provider_id == ("p3" if stage == "canary" else "p2")
    assert len(harness.closed) == 1


@pytest.mark.parametrize("switch_limit", [1, 2])
def test_stop_during_failed_canary_prevents_another_connection(switch_limit: int) -> None:
    class Harness(_Harness):
        def run_canary(self, pid: str, provider: ChatProvider | ProviderTransport) -> bool:
            self._stopped = True
            return super().run_canary(pid, provider)

    harness = Harness(order=["p1", "p2", "p3"], connect_fail={"p1"}, needs_canary={"p2"}, canary_fail={"p2"})
    harness.script = [_succeed()]
    runner = harness.make_runner(provider=None, provider_id="p1")
    runner.max_switches = switch_limit

    with pytest.raises(TaskCancelled):
        runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())

    assert harness.switched == ["p2"]
    assert len(harness.closed) == 1 and runner.provider is None
    assert not harness.specs


def test_stop_during_final_takeover_connect_failure_wins_over_provider_error() -> None:
    class Harness(_Harness):
        def connect(self, pid: str) -> ProviderTransport:
            if pid == "p2":
                self._stopped = True
            return super().connect(pid)

    harness = Harness(order=["p1", "p2"], connect_fail={"p1", "p2"})
    runner = harness.make_runner(provider=None, provider_id="p1")
    runner.max_switches = 1

    with pytest.raises(TaskCancelled):
        runner.run(task="t", turn_budget=5, fresh=True, handoff="", checkpoint=CheckpointView())

    assert harness.log[-1] == ("connect", "p2")
    assert not harness.specs
