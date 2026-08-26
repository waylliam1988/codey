from __future__ import annotations

import unittest
from unittest import mock

from codey.providers import timeouts as provider_timeouts


class ProviderTimeoutTests(unittest.TestCase):
    def test_navigation_and_ready_share_one_deadline(self) -> None:
        with mock.patch.object(
            provider_timeouts.time,
            "monotonic",
            side_effect=[100.0, 101.25, 102.5],
        ):
            deadline = provider_timeouts.start_deadline(5.0)
            navigation_ms = provider_timeouts.navigation_timeout_ms(deadline)
            ready_seconds = provider_timeouts.remaining(deadline, 90.0)

        self.assertEqual(deadline, 105.0)
        self.assertEqual(navigation_ms, 3750)
        self.assertEqual(ready_seconds, 2.5)

    def test_exhausted_deadline_fails_before_another_wait(self) -> None:
        with mock.patch.object(provider_timeouts.time, "monotonic", return_value=10.0):
            with self.assertRaisesRegex(TimeoutError, "budget was exhausted"):
                provider_timeouts.remaining(10.0, 90.0)


if __name__ == "__main__":
    unittest.main()