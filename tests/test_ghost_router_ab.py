from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.manual import ghost_router_ab
from tests.manual import ghost_router_production_ab


def test_self_test_router_beats_current_auto_baseline() -> None:
    payload = ghost_router_ab.run_cases(
        ghost_router_ab.FakeProvider(),
        provider_id="fake",
        cases=ghost_router_ab.load_cases(),
        timeout=1,
        new_chat_timeout=1,
    )

    assert payload["ok"]
    assert payload["summary"]["router"]["exact"] == payload["summary"]["router"]["total"]
    assert payload["summary"]["baseline"]["cost"] > payload["summary"]["router"]["cost"]


def test_production_spine_router_beats_current_auto_baseline() -> None:
    payload = ghost_router_production_ab.run_cases(
        provider_id="fake",
        cases=ghost_router_ab.load_cases(),
        router_provider_factory=lambda _provider_id: ghost_router_ab.FakeProvider(),
    )

    assert payload["ok"]
    assert payload["summary"]["router"]["exact"] == payload["summary"]["router"]["total"]
    assert payload["summary"]["baseline"]["cost"] > payload["summary"]["router"]["cost"]


def test_production_spine_writes_atomic_partial_progress(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "router_production.json"
        cases = ghost_router_ab.load_cases()[:1]
        calls = 0

        def fake_run_case(case, *, provider_id, arm, router_provider_factory):
            del case, provider_id, router_provider_factory
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
                "parse_ok": True,
            }

        monkeypatch.setattr(ghost_router_production_ab, "_run_case", fake_run_case)

        with pytest.raises(RuntimeError, match="interrupted"):
            ghost_router_production_ab.run_cases(
                provider_id="fake",
                cases=cases,
                router_provider_factory=lambda _provider_id: ghost_router_ab.FakeProvider(),
                output=output,
            )
        payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["complete"] is False
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["arm"] == "baseline"


def test_router_prompt_does_not_expose_internal_names() -> None:
    case = ghost_router_ab.RouterCase(
        name="plan",
        project=True,
        has_reviewable_diff=False,
        task="先别改代码，给我方案。",
        expected_mode="planning_readonly",
        risk="read",
    )

    prompt = ghost_router_ab.render_router_prompt(case)

    assert "Codey" not in prompt
    assert "Ghost" not in prompt
    assert "Routing is not permission" in prompt


def test_parse_router_reply_accepts_fenced_json_and_aliases() -> None:
    decision = ghost_router_ab.parse_router_reply(
        '```json\n{"mode":"planning","confidence":1.7,"reason":"readonly"}\n```'
    )

    assert decision.parse_ok
    assert decision.mode == "planning_readonly"
    assert decision.confidence == 1.0


def test_route_error_cost_marks_writer_confusion_as_severe() -> None:
    assert ghost_router_ab.route_error_cost("planning_readonly", "project_writer") >= 5
    assert ghost_router_ab.route_error_cost("chat", "planning_readonly") == 1


def test_router_subset_ok_allows_no_regression_control_case() -> None:
    case = next(item for item in ghost_router_ab.load_cases() if item.name == "plain_chat_no_project")

    payload = ghost_router_ab.run_cases(
        ghost_router_ab.FakeProvider(),
        provider_id="fake",
        cases=(case,),
        timeout=1,
        new_chat_timeout=1,
    )

    assert payload["ok"], payload
    assert payload["summary"]["delta"]["cost"] == 0


def test_load_cases_rejects_unknown_mode() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "cases.jsonl"
        path.write_text(
            json.dumps(
                {
                    "name": "bad",
                    "project": False,
                    "task": "hello",
                    "expected_mode": "magic",
                    "risk": "low",
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="invalid router case"):
            ghost_router_ab.load_cases(path)
