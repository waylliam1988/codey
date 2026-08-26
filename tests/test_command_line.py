from __future__ import annotations

import unittest

from codey.policies.command_line import split_run_command
from codey.providers.ids import normalize_provider_id, normalize_provider_ids


class SplitRunCommandTests(unittest.TestCase):
    def test_windows_backslash_paths_survive_tokenization(self) -> None:
        # Regression: posix-mode splitting ate backslashes, so approval risk
        # analysis and execution saw different argv on Windows.
        self.assertEqual(
            split_run_command("pytest C:\\codey\\tests\\test_x.py", platform="win32"),
            ["pytest", "C:\\codey\\tests\\test_x.py"],
        )

    def test_windows_quotes_are_stripped_after_splitting(self) -> None:
        self.assertEqual(
            split_run_command('pytest "C:/my dir/test_x.py" -q', platform="win32"),
            ["pytest", "C:/my dir/test_x.py", "-q"],
        )

    def test_posix_platform_keeps_posix_semantics(self) -> None:
        self.assertEqual(
            split_run_command("pytest 'a b' -q", platform="linux"),
            ["pytest", "a b", "-q"],
        )

    def test_default_platform_follows_the_host(self) -> None:
        argv = split_run_command("pytest -q")
        self.assertEqual(argv[0], "pytest")

    def test_unbalanced_quotes_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            split_run_command('echo "unbalanced', platform="win32")
        with self.assertRaises(ValueError):
            split_run_command('echo "unbalanced', platform="linux")


class NormalizeProviderIdTests(unittest.TestCase):
    def test_lowercases_and_trims(self) -> None:
        self.assertEqual(normalize_provider_id(" DeepSeek "), "deepseek")

    def test_rejects_non_identifier_shapes(self) -> None:
        self.assertEqual(normalize_provider_id(""), "")
        self.assertEqual(normalize_provider_id(None), "")
        self.assertEqual(normalize_provider_id("not a provider"), "")
        self.assertEqual(normalize_provider_id("gpt/4"), "")

    def test_hyphens_and_underscores_are_allowed(self) -> None:
        self.assertEqual(normalize_provider_id("open-router_x"), "open-router_x")

    def test_sequence_normalization_drops_duplicates_and_empties(self) -> None:
        self.assertEqual(
            normalize_provider_ids(["GLM", "", "glm", "deepseek", "bad id"]),
            ("glm", "deepseek"),
        )


if __name__ == "__main__":
    unittest.main()
