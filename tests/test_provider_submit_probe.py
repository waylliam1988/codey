from __future__ import annotations

import unittest
from unittest import mock

from tests.manual import provider_submit_probe


class ProviderSubmitProbeTests(unittest.TestCase):
    def _provider(self) -> mock.Mock:
        provider = mock.Mock()
        provider.session.page = object()
        provider.send.return_value = '{"ok":true}'
        return provider

    def test_keep_open_still_closes_reused_playwright_session(self) -> None:
        provider = self._provider()

        with (
            mock.patch.object(provider_submit_probe, "connect_provider", return_value=provider),
            mock.patch.object(provider_submit_probe, "connect_fresh_provider_tab") as fresh,
            mock.patch.object(provider_submit_probe, "_page_state", return_value={}),
            mock.patch.object(provider_submit_probe.provider_controls, "begin_task_context"),
            mock.patch.object(provider_submit_probe.provider_controls, "end_task_context"),
            mock.patch("builtins.print"),
        ):
            code = provider_submit_probe.main(["--provider", "mimo", "--keep-open", "hello"])

        self.assertEqual(code, 0)
        provider.close.assert_called_once_with()
        fresh.assert_not_called()

    def test_keep_open_preserves_fresh_provider_tab(self) -> None:
        provider = self._provider()

        with (
            mock.patch.object(provider_submit_probe, "connect_provider") as connect,
            mock.patch.object(provider_submit_probe, "connect_fresh_provider_tab", return_value=provider),
            mock.patch.object(provider_submit_probe, "_page_state", return_value={}),
            mock.patch.object(provider_submit_probe.provider_controls, "begin_task_context"),
            mock.patch.object(provider_submit_probe.provider_controls, "end_task_context"),
            mock.patch("builtins.print"),
        ):
            code = provider_submit_probe.main(
                ["--provider", "mimo", "--fresh", "--keep-open", "hello"]
            )

        self.assertEqual(code, 0)
        provider.close.assert_not_called()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
