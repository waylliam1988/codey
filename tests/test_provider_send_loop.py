from __future__ import annotations

import unittest
from unittest import mock

from codey import provider_flow, provider_send_loop as send_loop
from codey.provider_diagnostics import ResponseMissing
from codey.provider_submission import SendAttempt, SubmissionUncertain


class ProviderSendLoopTests(unittest.TestCase):
    def test_response_watch_starts_and_stops(self) -> None:
        page = object()
        with (
            mock.patch.object(send_loop.controls, "start_response_watch") as start,
            mock.patch.object(send_loop.controls, "stop_response_watch") as stop,
        ):
            with send_loop.response_watch(page, "glm"):
                start.assert_called_once_with(page, "glm")
                stop.assert_not_called()

        stop.assert_called_once_with(page, "glm")

    def test_response_watch_stops_after_error(self) -> None:
        page = object()
        with (
            mock.patch.object(send_loop.controls, "start_response_watch"),
            mock.patch.object(send_loop.controls, "stop_response_watch") as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with send_loop.response_watch(page, "glm"):
                    raise RuntimeError("boom")

        stop.assert_called_once_with(page, "glm")

    def test_context_records_text_progress_and_trace(self) -> None:
        ctx = send_loop.ProviderSendContext(
            page=object(),
            provider_id="glm",
            display_name="GLM",
            sent_at=10.0,
        )
        first = provider_flow.FlowObservation(response_nonempty=True)
        same = provider_flow.FlowObservation(
            response_nonempty=True,
            response_stable=True,
        )

        self.assertFalse(ctx.same_as_last("answer"))
        ctx.record_response("answer", first)
        self.assertTrue(ctx.appeared)
        self.assertEqual(ctx.last, "answer")
        self.assertEqual(ctx.stable, 0)
        self.assertTrue(ctx.same_as_last("answer"))

        ctx.record_response("answer", same)
        self.assertEqual(ctx.stable, 1)
        self.assertEqual(len(ctx.trace.snapshot()), 2)

    def test_reset_text_progress_keeps_flow_trace(self) -> None:
        ctx = send_loop.ProviderSendContext(
            page=object(),
            provider_id="glm",
            display_name="GLM",
            sent_at=10.0,
            appeared=True,
            last="answer",
            stable=2,
        )
        ctx.trace.add(provider_flow.FlowObservation(response_nonempty=True))

        ctx.reset_text_progress(sent_at=20.0)

        self.assertEqual(ctx.sent_at, 20.0)
        self.assertFalse(ctx.appeared)
        self.assertEqual(ctx.last, "")
        self.assertEqual(ctx.stable, 0)
        self.assertEqual(len(ctx.trace.snapshot()), 1)

    def test_completion_ready_delegates_to_provider_controls(self) -> None:
        page = object()
        ctx = send_loop.ProviderSendContext(page, "glm", "GLM", 0.0)
        observation = provider_flow.FlowObservation(response_nonempty=True)
        with mock.patch.object(
            send_loop.controls,
            "flow_stage_ready",
            return_value=True,
        ) as ready:
            result = send_loop.completion_ready(
                ctx,
                observation,
                built_in_ready=False,
                allow_recovery=True,
            )

        self.assertTrue(result)
        ready.assert_called_once_with(
            page,
            "glm",
            provider_flow.STAGE_COMPLETION,
            ctx.trace,
            observation,
            built_in_ready=False,
            allow_recovery=True,
        )

    def test_read_completion_delegates_to_flow_response_reader(self) -> None:
        ctx = send_loop.ProviderSendContext(object(), "glm", "GLM", 0.0)
        reader = mock.Mock(return_value="reply")
        with mock.patch.object(
            send_loop.controls,
            "read_flow_response",
            return_value="reply",
        ) as read:
            result = send_loop.read_completion(ctx, reader)

        self.assertEqual(result, "reply")
        read.assert_called_once_with(
            "glm",
            provider_flow.STAGE_COMPLETION,
            reader,
        )

    def test_recover_or_raise_returns_late_response_and_confirms(self) -> None:
        ctx = send_loop.ProviderSendContext(object(), "glm", "GLM", 0.0)
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with mock.patch.object(send_loop, "confirm_submission") as confirm:
            result = send_loop.recover_or_raise(
                ctx,
                attempt,
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value="late"),
                response_timeout=12.0,
                uncertain_message="uncertain",
            )

        self.assertEqual(result, "late")
        confirm.assert_called_once_with(attempt, "glm")

    def test_recover_or_raise_returns_final_when_response_appeared(self) -> None:
        ctx = send_loop.ProviderSendContext(
            object(),
            "glm",
            "GLM",
            0.0,
            appeared=True,
            last="partial",
        )
        read_final = mock.Mock(return_value="final")
        with mock.patch.object(send_loop.controls, "recover_response") as recover:
            result = send_loop.recover_or_raise(
                ctx,
                SendAttempt(phase="confirmed", method="click"),
                read_final=read_final,
                read_late=mock.Mock(return_value=""),
                response_timeout=12.0,
                uncertain_message="uncertain",
            )

        self.assertEqual(result, "final")
        read_final.assert_called_once_with()
        recover.assert_not_called()

    def test_recover_or_raise_confirms_recovered_response(self) -> None:
        ctx = send_loop.ProviderSendContext(object(), "glm", "GLM", 0.0)
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(
                send_loop.controls,
                "recover_response",
                return_value="recovered",
            ) as recover,
            mock.patch.object(send_loop, "confirm_submission") as confirm,
        ):
            result = send_loop.recover_or_raise(
                ctx,
                attempt,
                read_final=mock.Mock(),
                read_late=mock.Mock(return_value=""),
                response_timeout=12.0,
                uncertain_message="uncertain",
            )

        self.assertEqual(result, "recovered")
        recover.assert_called_once()
        confirm.assert_called_once_with(attempt, "glm")

    def test_recover_or_raise_rejects_failed_unconfirmed_click(self) -> None:
        ctx = send_loop.ProviderSendContext(object(), "glm", "GLM", 0.0)
        attempt = SendAttempt()
        attempt.submit("click", lambda: (_ for _ in ()).throw(RuntimeError("click")))
        with (
            mock.patch.object(send_loop.controls, "recover_response", return_value=None),
            mock.patch.object(send_loop.controls, "reject_control") as reject,
        ):
            with self.assertRaisesRegex(SubmissionUncertain, "uncertain"):
                send_loop.recover_or_raise(
                    ctx,
                    attempt,
                    read_final=mock.Mock(),
                    read_late=mock.Mock(return_value=""),
                    response_timeout=12.0,
                    uncertain_message="uncertain",
                )

        reject.assert_called_once_with("glm", send_loop.controls.CONTROL_SEND_BUTTON)

    def test_recover_or_raise_raises_response_missing_after_confirmed_timeout(self) -> None:
        ctx = send_loop.ProviderSendContext(object(), "glm", "GLM", 12.0)
        with mock.patch.object(send_loop.controls, "recover_response", return_value=None):
            with self.assertRaisesRegex(ResponseMissing, "GLM response timed out after 7s"):
                send_loop.recover_or_raise(
                    ctx,
                    SendAttempt(phase="confirmed", method="click"),
                    read_final=mock.Mock(),
                    read_late=mock.Mock(return_value=""),
                    response_timeout=7.0,
                    uncertain_message="uncertain",
                )


if __name__ == "__main__":
    unittest.main()
