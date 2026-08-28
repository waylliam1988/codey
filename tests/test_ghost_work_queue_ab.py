from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.manual import ghost_work_queue_production_ab


def test_production_spine_work_queue_beats_no_queue_baseline() -> None:
    payload = ghost_work_queue_production_ab.run_cases(
        provider_id="fake",
        provider_factory=lambda _provider_id: ghost_work_queue_production_ab._MainProvider(),
    )

    assert payload["ok"]
    assert payload["summary"]["queue"]["exact"] == payload["summary"]["queue"]["total"]
    assert payload["summary"]["baseline"]["cost"] > payload["summary"]["queue"]["cost"]


def test_work_queue_control_case_allows_no_regression() -> None:
    case = next(item for item in ghost_work_queue_production_ab.DEFAULT_CASES if item.name == "no-queue-continue-chat")

    payload = ghost_work_queue_production_ab.run_cases(
        provider_id="fake",
        cases=(case,),
        provider_factory=lambda _provider_id: ghost_work_queue_production_ab._MainProvider(),
    )

    assert payload["ok"], payload
    assert payload["summary"]["delta"]["cost"] == 0


def test_work_queue_production_spine_writes_atomic_partial_progress(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "work_queue_production.json"
        cases = ghost_work_queue_production_ab.DEFAULT_CASES[:1]
        calls = 0

        def fake_run_case(case, *, provider_id, arm, provider_factory):
            del case, provider_id, provider_factory
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted")
            return {
                "arm": arm,
                "expected_mode": "chat",
                "observed_mode": "chat",
                "exact": True,
                "error_cost": 0,
                "severe_error": False,
            }

        monkeypatch.setattr(ghost_work_queue_production_ab, "_run_case", fake_run_case)

        with pytest.raises(RuntimeError, match="interrupted"):
            ghost_work_queue_production_ab.run_cases(
                provider_id="fake",
                cases=cases,
                provider_factory=lambda _provider_id: ghost_work_queue_production_ab._MainProvider(),
                output=output,
            )
        payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["complete"] is False
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["arm"] == "baseline"
