"""The live-model judge must reject wrong outputs without asking a model."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from tools import local_model_release_gate as gate
from tools.local_model_gate_attempts import GateTarget


@pytest.mark.parametrize("reply", ["NOT_KOBOLD_OK", "KOBOLD_OK plus explanation", ""])
def test_chat_requires_exact_marker(reply, tmp_path):
    provider = SimpleNamespace(send=lambda *a, **kw: reply, close=lambda: None)
    with (
        mock.patch.object(gate, "probe_endpoint", return_value=("http://model.test/v1", ("test-model",))),
        mock.patch.object(gate.attempts, "make_provider", return_value=provider),
    ):
        assert gate.run_chat_case(GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000), tmp_path)["ok"] is False


@pytest.mark.parametrize("reply", ["KOBOLD_OK", " KOBOLD_OK\n"])
def test_chat_accepts_marker_and_transport_whitespace(reply, tmp_path):
    provider = SimpleNamespace(send=lambda *a, **kw: reply, close=lambda: None)
    with (
        mock.patch.object(gate, "probe_endpoint", return_value=("http://model.test/v1", ("test-model",))),
        mock.patch.object(gate.attempts, "make_provider", return_value=provider),
    ):
        assert gate.run_chat_case(GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000), tmp_path)["ok"] is True


@pytest.mark.parametrize("implementation,expected", [
    ("return 5", False), ("return a + b", True),
])
def test_create_checks_more_than_visible_example(tmp_path, implementation, expected):
    (tmp_path / "math_utils.py").write_text(f"def add(a, b):\n    {implementation}\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_math_utils.py").write_text(
        "import unittest\nfrom math_utils import add\n"
        "class Tests(unittest.TestCase):\n    def test_example(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    assert gate._verify_fixture(tmp_path, "create")["ok"] is expected


@pytest.mark.parametrize("implementation,expected", [
    ("return 80", False), ("return price * (1 - percent / 100)", True),
])
def test_edit_checks_more_than_visible_example(tmp_path, implementation, expected):
    gate._make_fixture(tmp_path, "edit")
    (tmp_path / "pricing.py").write_text(
        f"def discounted_price(price, percent):\n    {implementation}\n", encoding="utf-8",
    )
    assert gate._verify_fixture(tmp_path, "edit")["ok"] is expected


def test_existing_tests_cannot_be_rewritten_to_pass(tmp_path):
    gate._make_fixture(tmp_path, "edit")
    baseline = gate.fixture_test_hashes(tmp_path)
    (tmp_path / "pricing.py").write_text(
        "def discounted_price(price, percent):\n    return price * (1 - percent / 100)\n", encoding="utf-8",
    )
    (tmp_path / "tests/test_pricing.py").write_text(
        "import unittest\nclass Tests(unittest.TestCase):\n    def test_fake(self):\n        pass\n",
        encoding="utf-8",
    )
    verdict = gate._verify_fixture(tmp_path, "edit", baseline_tests=baseline)
    assert verdict["ok"] is False
    assert "changed" in verdict["output"]


def test_create_requires_a_test_that_rejects_wrong_addition(tmp_path):
    (tmp_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_math_utils.py").write_text(
        "import unittest\nclass Tests(unittest.TestCase):\n    def test_fake(self):\n        pass\n", encoding="utf-8",
    )
    assert gate._verify_fixture(tmp_path, "create")["ok"] is False
