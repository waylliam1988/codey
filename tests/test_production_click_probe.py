"""Fixed source prefix still executes production effects before model clicks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_production_navigation_ab import fixed_flow
from tests.test_task_contract_fixed_replies import TARGET, install_original_http
from tools.production_click_probe import run_probe
from tools.production_navigation_ab import production_cases, read_jsonl

pytestmark = pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


@pytest.mark.parametrize("index", [2, 3])
def test_only_source_prefix_is_scripted_and_followup_uses_original_click_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int,
) -> None:
    scenario, expected = production_cases()[index]
    actions = fixed_flow(scenario, expected, "A", index)[2:]
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    result = run_probe(directory, scenario, expected, TARGET)
    assert result["passed"] is True
    assert result["source_selection_live"] is False
    provider = read_jsonl(directory / "provider.jsonl")
    assert sum(row["type"] == "scripted_acquisition" for row in provider) == 1
    # No fake model request is counted for the source prefix; later requests
    # go through the ordinary provider path (scripted HTTP only in pytest).
    assert result["model_calls"] == 2
    receipts = read_jsonl(directory / "click-observations.jsonl")
    assert len(receipts) == 1 and receipts[0]["ok"] is True
