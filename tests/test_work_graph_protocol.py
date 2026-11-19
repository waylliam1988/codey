"""Guard grading corrections and the separate guided study's information boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject
from tools.agent_work_graph_guided_probe import GUIDANCE, cases
from tools.agent_work_graph_query_probe import cases as optional_cases
from tools.audit_work_graph_query_probe import audit, current_pytest_pass


@pytest.mark.parametrize("override", [{"current": False}, {"exit_code": 1}, {"ok": False},
    {"command": "python -m pytest -q tests/test_smoke.py"}, {"output": "no tests ran"}])
def test_current_full_suite_gate_rejects_inadequate_evidence(override: JsonObject) -> None:
    valid: JsonObject = {"current": True, "exit_code": 0, "ok": True, "command": "python -m pytest -q", "output": "2 passed"}
    assert current_pytest_pass([valid])
    assert not current_pytest_pass([{**valid, **override}])
    assert not current_pytest_pass([valid, {**valid, "exit_code": 1}])


def test_audit_restores_only_output_and_preserves_original_summary(tmp_path: Path) -> None:
    cell = tmp_path / "r0-checkout-graph"
    cell.mkdir()
    original: JsonObject = {"arm": "graph", "objective_success": True, "stop_reason": "done", "success": False,
        "product_success": True, "production_current_pytest_pass": False, "exception": None}
    (cell / "summary.json").write_text(json.dumps(original), encoding="utf-8")
    before = (cell / "summary.json").read_bytes()
    (cell / "production-checks.json").write_text(json.dumps([{"effect_id": "actual", "command": "python -m pytest -q",
        "exit_code": 0, "ok": True, "current": True, "output": ""}]), encoding="utf-8")
    (cell / "final-graph.json").write_text(json.dumps({"nodes": {"execution:actual": {
        "text": "command=python -m pytest -q\nstatus=passed; exit=0; matches_current_workspace=True\n2 passed"}}}), encoding="utf-8")
    (cell / "query-receipts.json").write_text("[]", encoding="utf-8")
    audited = audit(tmp_path)
    assert '"success": true' in json.dumps(audited)
    assert (cell / "summary.json").read_bytes() == before
    # Text cannot promote stale trusted workspace evidence to current evidence.
    (cell / "production-checks.json").write_text(json.dumps([{"effect_id": "actual", "command": "python -m pytest -q",
        "exit_code": 0, "ok": True, "current": False, "output": ""}]), encoding="utf-8")
    assert '"success": true' not in json.dumps(audit(tmp_path))


def test_guided_protocol_changes_instructions_without_adding_answers() -> None:
    originals = optional_cases()
    for name, case in cases().items():
        original = originals[name]
        assert case.files == original.files and case.pages == original.pages
        assert case.grader == original.grader and case.allowed_changes == original.allowed_changes
        assert case.task.endswith(GUIDANCE)
        assert "Modify source only" not in case.task
        for hidden in ("ROUND_HALF_UP", "ASCII decimal digits", "booleans_rejected"):
            assert hidden not in GUIDANCE
