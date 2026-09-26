"""Parity locks for wait_for_stable_completion split (base.py only).

Covers poll stages: observe/min_wait, JSON fast paths, completion_ready,
and post-timeout recovery. All tests mock time/cancellation for determinism
and assert current (pre-split) behavior so the pure-extraction refactor can
prove zero behavior change (green before AND after).
"""

from __future__ import annotations

import unittest
from unittest import mock

from codey.providers import send_loop
from codey.providers.diagnostics import ResponseMissing
from codey.providers.submission import SendAttempt, SubmissionUncertain
from codey.providers.web_drivers import base as driver_base


def _ctx(sent_at: float = 1000.0, **kw) -> send_loop.ProviderSendContext:
    base = {"page": object(), "provider_id": "glm", "display_name": "GLM", "sent_at": sent_at}
    base.update(kw)
    return send_loop.ProviderSendContext(**base)  # type: ignore[arg-type]


def _confirmed_attempt() -> SendAttempt:
    attempt = SendAttempt()
    attempt.submit("click", lambda: None)
    attempt.confirm()
    return attempt


class StablePollTests(unittest.TestCase):
    def test_stable_response_returns_final_and_calls_before_return(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        before_return = mock.Mock()
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.send_loop, "completion_ready", return_value=True),
            mock.patch.object(
                driver_base.send_loop, "read_completion", return_value="done"
            ) as read_completion,
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=5.0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(side_effect=["hi", "hi", "hi", "hi"]),
                read_final=mock.Mock(return_value="raw-final"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
                before_return=before_return,
            )
        self.assertEqual(result, "done")
        before_return.assert_called_once_with()
        # finish() must route through send_loop.read_completion, not raw reader.
        read_completion.assert_called_once()
        self.assertTrue(attempt.confirmed)

    def test_empty_polls_are_skipped_without_confirm_side_effect_change(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        read_current = mock.Mock(side_effect=["", "", "hi", "hi", "hi", "hi"])
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.send_loop, "completion_ready", return_value=True),
            mock.patch.object(driver_base.send_loop, "read_completion", return_value="done"),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=5.0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=read_current,
                read_final=mock.Mock(return_value="raw"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
            )
        self.assertEqual(result, "done")
        self.assertEqual(ctx.last, "hi")

    def test_min_wait_blocks_early_stable_return(self) -> None:
        clock = {"now": 1000.0}
        ctx = _ctx(sent_at=1000.0)
        attempt = _confirmed_attempt()

        def now() -> float:
            return clock["now"]

        def wait(tick: float) -> None:
            clock["now"] += 1.0

        # min_wait=10: first stable polls (t=1001,1002) still < min_wait, must continue;
        # clock advances each tick so eventually (t>=1010) it returns.
        read_current = mock.Mock(return_value="steady")
        with (
            mock.patch.object(driver_base.time, "time", side_effect=now),
            mock.patch.object(driver_base.cancellation, "wait", side_effect=wait),
            mock.patch.object(driver_base.send_loop, "completion_ready", return_value=True),
            mock.patch.object(driver_base.send_loop, "read_completion", return_value="done"),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=60.0,
                stable_ticks=1,
                tick=1.0,
                min_wait=10.0,
                read_current=read_current,
                read_final=mock.Mock(return_value="raw"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
            )
        self.assertEqual(result, "done")
        # Must have polled more than the minimum stable ticks because min_wait forced waiting.
        self.assertGreaterEqual(read_current.call_count, 10)

    def test_before_poll_true_skips_read(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        read_current = mock.Mock(side_effect=["hi", "hi", "hi"])
        before_poll = mock.Mock(side_effect=[True, True, False, False, False, False])
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.send_loop, "completion_ready", return_value=True),
            mock.patch.object(driver_base.send_loop, "read_completion", return_value="done"),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=5.0,
                stable_ticks=1,
                tick=0,
                min_wait=0.0,
                read_current=read_current,
                read_final=mock.Mock(return_value="raw"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
                before_poll=before_poll,
            )
        self.assertEqual(result, "done")
        # Two skips: read_current called fewer times than total polls.
        self.assertGreaterEqual(before_poll.call_count, 3)
        self.assertLess(read_current.call_count, before_poll.call_count)


class JsonToolTests(unittest.TestCase):
    def test_valid_json_returns_before_general_stable_ticks(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        reply = '{"tool":"done","args":{}}'
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.send_loop, "completion_ready") as ready,
            mock.patch.object(driver_base.send_loop, "read_completion", return_value=reply),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=5.0,
                stable_ticks=99,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=reply),
                read_final=mock.Mock(return_value=reply),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
                is_json_tool=lambda s: True,
                json_tool_stable_ticks=2,
            )
        self.assertEqual(result, reply)
        self.assertLess(ready.call_count, 3)

    def test_repairable_json_waits_for_stable_ticks_then_returns(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        incomplete = '{"tool":"done"'
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.send_loop, "completion_ready", return_value=True),
            mock.patch.object(driver_base.send_loop, "read_completion", return_value="fixed"),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=5.0,
                stable_ticks=3,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=incomplete),
                read_final=mock.Mock(return_value="fixed"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
                is_json_tool=lambda s: False,
                looks_like_json_tool=lambda s: True,
                repair_json_tool=lambda s: s + "}",
                json_tool_stable_ticks=2,
            )
        self.assertEqual(result, "fixed")

    def test_unrepairable_looks_like_json_falls_through_to_ready(self) -> None:
        # Current behavior lock: looks-like-JSON but unrepairable still consults
        # completion_ready instead of hanging. Deterministic parity, not a bug fix.
        ctx = _ctx()
        attempt = _confirmed_attempt()
        weird = "{not-json"
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(
                driver_base.send_loop, "completion_ready", return_value=True
            ) as ready,
            mock.patch.object(driver_base.send_loop, "read_completion", return_value="out"),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=5.0,
                stable_ticks=1,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(side_effect=[weird, weird, weird]),
                read_final=mock.Mock(return_value="raw"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
                is_json_tool=lambda s: False,
                looks_like_json_tool=lambda s: True,
                repair_json_tool=lambda s: "",
                json_tool_stable_ticks=2,
            )
        self.assertEqual(result, "out")
        self.assertGreaterEqual(ready.call_count, 1)


class TimeoutRecoveryTests(unittest.TestCase):
    def test_late_response_wins_and_confirms(self) -> None:
        ctx = _ctx()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        before_return = mock.Mock()
        with mock.patch.object(driver_base.cancellation, "wait"):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=""),
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value="late"),
                uncertain_message="uncertain",
                before_return=before_return,
            )
        self.assertEqual(result, "late")
        self.assertTrue(attempt.confirmed)
        before_return.assert_called_once_with()

    def test_appeared_last_returns_final(self) -> None:
        ctx = _ctx(appeared=True, last="partial")
        attempt = _confirmed_attempt()
        with mock.patch.object(driver_base.cancellation, "wait"):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=""),
                read_final=mock.Mock(return_value="final"),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
            )
        self.assertEqual(result, "final")

    def test_recovered_response_confirms(self) -> None:
        ctx = _ctx()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.controls, "recover_response", return_value="rec"),
        ):
            result = driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=""),
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
            )
        self.assertEqual(result, "rec")
        self.assertTrue(attempt.confirmed)

    def test_unconfirmed_click_raises_uncertain_and_rejects(self) -> None:
        ctx = _ctx()
        attempt = SendAttempt()
        attempt.submit("click", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.controls, "recover_response", return_value=None),
            mock.patch.object(driver_base.controls, "reject_control") as reject,
            self.assertRaises(SubmissionUncertain),
        ):
            driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=""),
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
            )
        reject.assert_called_once_with("glm", driver_base.controls.CONTROL_SEND_BUTTON)

    def test_confirmed_timeout_raises_missing(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.controls, "recover_response", return_value=None),
            self.assertRaises(ResponseMissing),
        ):
            driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=""),
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
            )

    def test_before_recover_hook_runs_on_timeout(self) -> None:
        ctx = _ctx()
        attempt = _confirmed_attempt()
        before_recover = mock.Mock()
        with (
            mock.patch.object(driver_base.cancellation, "wait"),
            mock.patch.object(driver_base.controls, "recover_response", return_value=None),
            self.assertRaises(ResponseMissing),
        ):
            driver_base.wait_for_stable_completion(
                ctx,
                attempt,
                response_timeout=0,
                stable_ticks=2,
                tick=0,
                min_wait=0.0,
                read_current=mock.Mock(return_value=""),
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value=""),
                uncertain_message="uncertain",
                before_recover=before_recover,
            )
        before_recover.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
