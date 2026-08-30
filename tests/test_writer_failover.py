"""Pure unit tests for the Writer failover state machine.

These exercise ``WriterFailoverRunner`` in isolation with plain fakes, covering
the nine behaviours the state machine must guarantee before it replaces the
``TaskRunner`` closures.
"""

from __future__ import annotations

import unittest

from codey.agents.runner import RunResult
from codey.runtime.cancellation import TaskCancelled
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.agents.writer_failover import (
    CheckpointView,
    WriterAttempt,
    WriterFailoverRunner,
)


def _failure(model: str = "m", action: str = "task") -> ProviderFailure:
    return ProviderFailure(
        model=model,
        action=action,
        url="",
        title="",
        message="boom",
        time="t",
    )


def _ok(turns: int = 1) -> RunResult:
    return RunResult(summary="ok", stop_reason="done", turns=turns)


def _succeed(turns: int = 1):
    def action(spec: WriterAttempt, note) -> RunResult:
        return _ok(turns)

    return action


def _fail(turn: int = 1):
    def action(spec: WriterAttempt, note) -> RunResult:
        note(turn)
        raise ProviderActionError(_failure(model=spec.provider_id))

    return action


class _Harness:
    """Records every provider/checkpoint hook and scripts each Writer attempt."""

    def __init__(
        self,
        *,
        order,
        connect_fail=(),
        canary_fail=(),
        needs_canary=(),
        stopped=False,
    ) -> None:
        self.order = list(order)
        self.connect_fail = set(connect_fail)
        self.canary_fail = set(canary_fail)
        self.needs_canary_ids = set(needs_canary)
        self._stopped = stopped
        self.log: list[tuple[str, str]] = []
        self.closed: list[object] = []
        self.switched: list[str] = []
        self.excluded_snapshots: list[set[str]] = []
        self.refreshed = 0
        self.view = CheckpointView(
            prompt="refreshed",
            changed_files=("r.py",),
            successful_checks=(),
        )
        self.specs: list[WriterAttempt] = []
        self.script: list = []

    def select_next(self, excluded):
        self.excluded_snapshots.append(set(excluded))
        for pid in self.order:
            if pid not in excluded:
                return pid
        return None

    def connect(self, pid):
        self.log.append(("connect", pid))
        if pid in self.connect_fail:
            raise RuntimeError(f"connect {pid} failed")
        return {"provider": pid}

    def close(self, provider):
        self.closed.append(provider)

    def needs_canary(self, pid):
        return pid in self.needs_canary_ids

    def run_canary(self, pid, provider):
        return pid not in self.canary_fail

    def capture_failure(self, pid, action, error):
        return _failure(model=pid, action=action)

    def record_failure(self, pid, failure):
        self.log.append(("record_failure", pid))

    def record_success(self, pid):
        self.log.append(("record_success", pid))

    def clear_session(self, pid):
        self.log.append(("clear_session", pid))

    def on_switch(self, pid):
        self.switched.append(pid)

    def refresh_checkpoint(self):
        self.refreshed += 1
        return self.view

    def stopped(self):
        return self._stopped

    def attempt(self, spec, note):
        self.specs.append(spec)
        action = self.script.pop(0)
        return action(spec, note)

    def make_runner(self, *, provider, provider_id, switches=0, tried=None):
        return WriterFailoverRunner(
            provider=provider,
            provider_id=provider_id,
            switches=switches,
            tried=set(tried if tried is not None else {provider_id}),
            attempt=self.attempt,
            select_next=self.select_next,
            connect=self.connect,
            close=self.close,
            needs_canary=self.needs_canary,
            run_canary=self.run_canary,
            capture_failure=self.capture_failure,
            record_failure=self.record_failure,
            record_success=self.record_success,
            clear_session=self.clear_session,
            on_switch=self.on_switch,
            refresh_checkpoint=self.refresh_checkpoint,
            stopped=self.stopped,
        )


class WriterFailoverRunnerTests(unittest.TestCase):
    def test_first_provider_succeeds_without_switch(self) -> None:
        h = _Harness(order=["p1", "p2"])
        h.script = [_succeed(turns=2)]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        result = runner.run(
            task="t",
            turn_budget=5,
            fresh=False,
            handoff="H",
            checkpoint=CheckpointView(prompt="init"),
        )

        self.assertEqual(result.turns, 2)
        self.assertEqual(h.switched, [])
        self.assertIn(("record_success", "p1"), h.log)
        spec = h.specs[0]
        self.assertFalse(spec.strict_fresh_chat)
        self.assertFalse(spec.fresh_chat)
        self.assertEqual(spec.handoff, "H")
        self.assertEqual(spec.checkpoint.prompt, "init")
        self.assertEqual(runner.provider_id, "p1")

    def test_initial_connect_failure_switches_with_strict_fresh_chat(self) -> None:
        h = _Harness(order=["p1", "p2"], connect_fail={"p1"})
        h.script = [_succeed()]
        runner = h.make_runner(provider=None, provider_id="p1")

        runner.run(
            task="t",
            turn_budget=5,
            fresh=False,
            handoff="H",
            checkpoint=CheckpointView(prompt="init"),
        )

        self.assertEqual(h.switched, ["p2"])
        self.assertEqual(runner.provider_id, "p2")
        self.assertEqual(h.refreshed, 1)
        spec = h.specs[0]
        self.assertTrue(spec.strict_fresh_chat)
        self.assertTrue(spec.fresh_chat)
        self.assertEqual(spec.checkpoint, h.view)
        # An initial reconnect keeps the incoming handoff (unlike a mid-attempt drop).
        self.assertEqual(spec.handoff, "H")

    def test_switch_gives_next_writer_only_remaining_turns(self) -> None:
        h = _Harness(order=["p1", "p2"])
        h.script = [_fail(turn=3), _succeed(turns=2)]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        result = runner.run(
            task="t",
            turn_budget=10,
            fresh=True,
            handoff="H",
            checkpoint=CheckpointView(prompt="init"),
        )

        self.assertEqual(h.specs[1].remaining_turns, 7)
        self.assertEqual(result.turns, 5)
        # A mid-attempt drop clears the handoff and forces a fresh chat.
        self.assertEqual(h.specs[1].handoff, "")
        self.assertTrue(h.specs[1].fresh_chat)
        self.assertEqual(h.specs[1].checkpoint, h.view)

    def test_mid_attempt_failure_excludes_just_failed_provider(self) -> None:
        h = _Harness(order=["p1", "p2"])
        h.script = [_fail(turn=1), _succeed()]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1", tried=())

        runner.run(
            task="t",
            turn_budget=5,
            fresh=True,
            handoff="H",
            checkpoint=CheckpointView(prompt="init"),
        )

        self.assertIn("p1", h.excluded_snapshots[0])
        self.assertEqual(h.switched, ["p2"])
        self.assertEqual(runner.provider_id, "p2")

    def test_stop_takes_priority_over_sibling(self) -> None:
        h = _Harness(order=["p1", "p2"], stopped=True)
        h.script = [_fail(turn=1)]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        with self.assertRaises(TaskCancelled):
            runner.run(
                task="t",
                turn_budget=5,
                fresh=True,
                handoff="",
                checkpoint=CheckpointView(),
            )

        self.assertEqual(h.switched, [])
        self.assertNotIn(("record_failure", "p1"), h.log)

    def test_canary_failure_skips_provider(self) -> None:
        h = _Harness(
            order=["p1", "p2", "p3"],
            connect_fail={"p1"},
            needs_canary={"p2"},
            canary_fail={"p2"},
        )
        h.script = [_succeed()]
        runner = h.make_runner(provider=None, provider_id="p1")

        runner.run(
            task="t",
            turn_budget=5,
            fresh=False,
            handoff="",
            checkpoint=CheckpointView(),
        )

        self.assertEqual(runner.provider_id, "p3")
        self.assertIn({"provider": "p2"}, h.closed)
        self.assertEqual(h.switched, ["p2", "p3"])

    def test_terminal_canary_failure_leaves_no_closed_provider(self) -> None:
        # A canary failure at the switch budget raises; the runner must not
        # keep a closed provider around, or a later run on this shared
        # instance would skip reconnect and use a dead provider once.
        h = _Harness(
            order=["p1", "p2"],
            connect_fail={"p1"},
            needs_canary={"p2"},
            canary_fail={"p2"},
        )
        h.script = []
        runner = h.make_runner(provider=None, provider_id="p1")
        runner.max_switches = 1

        with self.assertRaises(ProviderActionError):
            runner.run(
                task="t",
                turn_budget=5,
                fresh=False,
                handoff="",
                checkpoint=CheckpointView(),
            )

        self.assertEqual(runner.provider_id, "p2")
        self.assertIn({"provider": "p2"}, h.closed)
        self.assertIsNone(runner.provider)

    def test_mid_attempt_close_always_clears_provider_reference(self) -> None:
        h = _Harness(order=["p1", "p2", "p3", "p4"])
        h.script = [_fail(turn=1), _fail(turn=1), _fail(turn=1)]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        with self.assertRaises(ProviderActionError):
            runner.run(
                task="t",
                turn_budget=100,
                fresh=True,
                handoff="",
                checkpoint=CheckpointView(),
            )

        self.assertEqual(h.switched, ["p2", "p3"])
        self.assertIsNone(runner.provider)

    def test_switches_at_most_twice(self) -> None:
        h = _Harness(order=["p1", "p2", "p3", "p4"])
        h.script = [_fail(turn=1), _fail(turn=1), _fail(turn=1)]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        with self.assertRaises(ProviderActionError):
            runner.run(
                task="t",
                turn_budget=100,
                fresh=True,
                handoff="",
                checkpoint=CheckpointView(),
            )

        self.assertEqual(h.switched, ["p2", "p3"])
        self.assertNotIn("p4", h.switched)

    def test_checkpoint_view_refreshed_after_switch(self) -> None:
        h = _Harness(order=["p1", "p2"])
        h.view = CheckpointView(prompt="fresh-local", changed_files=("x.py",))
        h.script = [_fail(turn=1), _succeed()]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        runner.run(
            task="t",
            turn_budget=10,
            fresh=True,
            handoff="H",
            checkpoint=CheckpointView(prompt="stale"),
        )

        self.assertEqual(h.specs[0].checkpoint.prompt, "stale")
        self.assertEqual(h.specs[1].checkpoint, h.view)
        self.assertEqual(h.refreshed, 1)

    def test_review_repair_shares_switch_budget(self) -> None:
        h = _Harness(order=["p1", "p2", "p3"])
        h.script = [_fail(turn=1), _succeed()]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        runner.run(
            task="init",
            turn_budget=10,
            fresh=True,
            handoff="",
            checkpoint=CheckpointView(),
        )
        self.assertEqual(runner.switches, 1)
        self.assertEqual(runner.provider_id, "p2")

        # Review closes the provider before the repair attempt.
        runner.provider = None
        h.script = [_fail(turn=1), _fail(turn=1)]
        with self.assertRaises(ProviderActionError):
            runner.run(
                task="repair",
                turn_budget=10,
                fresh=False,
                handoff="",
                checkpoint=CheckpointView(),
            )

        # The repair only gets the one remaining switch (p2 -> p3).
        self.assertEqual(runner.switches, 2)
        self.assertEqual(h.switched, ["p2", "p3"])

    def test_failed_provider_is_never_reselected(self) -> None:
        h = _Harness(order=["p1", "p2", "p3", "p4"])
        h.script = [_fail(turn=1), _fail(turn=1), _succeed()]
        runner = h.make_runner(provider={"p": "p1"}, provider_id="p1")

        runner.run(
            task="t",
            turn_budget=100,
            fresh=True,
            handoff="",
            checkpoint=CheckpointView(),
        )

        self.assertEqual(runner.provider_id, "p3")
        self.assertEqual(h.switched, ["p2", "p3"])
        self.assertEqual(sorted(runner.tried), ["p1", "p2", "p3"])


if __name__ == "__main__":
    unittest.main()
