"""A required machine-contract check skipped by pytest is not a green gate."""

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("skipped,exit_code,expected", [(True, 0, 1), (False, 0, 0), (False, 2, 2)])
def test_machine_gate_keeps_failures_and_rejects_skips(monkeypatch, skipped, exit_code, expected):
    from tools.machine_contract_gate import run_gate

    def run(args, plugins):
        assert "tests/test_event_outputs_share_run_identity.py" in args
        plugins[0].pytest_runtest_logreport(SimpleNamespace(skipped=skipped, nodeid="required_case"))
        return exit_code

    monkeypatch.setattr(pytest, "main", run)
    assert run_gate() == expected
