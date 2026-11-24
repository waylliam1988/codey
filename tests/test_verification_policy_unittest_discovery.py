from pathlib import Path
from unittest.mock import patch

from codey.completion.verification_policy import discover_verification_candidates


def test_unittest_fallback_uses_default_discovery_filename_pattern(tmp_path: Path) -> None:
    (tmp_path / "calculator_test.py").write_text("import unittest\n", encoding="utf-8")

    with patch("codey.completion.verification_policy.shutil.which", return_value="python"):
        suffix_only = discover_verification_candidates(tmp_path)
        (tmp_path / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
        prefixed = discover_verification_candidates(tmp_path)

    assert all(candidate.command != "python -m unittest discover" for candidate in suffix_only)
    assert any(candidate.command == "python -m unittest discover" for candidate in prefixed)
