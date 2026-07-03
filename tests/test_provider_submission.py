from __future__ import annotations

import unittest

from codey.provider_submission import SendAttempt


class SendAttemptTests(unittest.TestCase):
    def test_marks_attempted_before_remote_action_and_confirms(self) -> None:
        attempt = SendAttempt()
        phases = []

        attempt.submit("click", lambda: phases.append(attempt.phase))
        attempt.confirm()

        self.assertEqual(phases, ["attempted"])
        self.assertEqual(attempt.method, "click")
        self.assertTrue(attempt.confirmed)

    def test_failed_remote_action_becomes_uncertain_and_forbids_second_submission(self) -> None:
        attempt = SendAttempt()
        calls = []

        attempt.submit("click", lambda: (_ for _ in ()).throw(ValueError("detached")))
        with self.assertRaisesRegex(RuntimeError, "already attempted"):
            attempt.submit("enter", lambda: calls.append("second"))

        self.assertEqual(attempt.phase, "attempted")
        self.assertIsInstance(attempt.action_error, ValueError)
        self.assertEqual(calls, [])

    def test_confirmation_callback_runs_once_for_click(self) -> None:
        attempt = SendAttempt()
        calls = []
        attempt.submit("click", lambda: None)

        attempt.confirm(lambda: calls.append("confirmed"))
        attempt.confirm(lambda: calls.append("again"))

        self.assertEqual(calls, ["confirmed"])


if __name__ == "__main__":
    unittest.main()
