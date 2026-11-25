"""Exact typed values use the existing receipt-bound copy and edit path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.operations.browser.browser_copy import CopyTurn, copy_target
from codey.types.json_value import JsonObject
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_typed_copy_live_gate import copy_case, run_copy
from tools.completion_explanation_ab import Target


@pytest.mark.parametrize("name", ["decimal", "precision", "string", "multi", "renamed"])
def test_copy_contract_freezes_and_restores_typed_destinations(name: str) -> None:
    case = copy_case(name)
    target = copy_target(case.task)
    assert target is not None
    frozen = CopyTurn(target)
    assert CopyTurn.restore(frozen.payload()) == frozen
    if name == "multi":
        assert target.fields == (("balance", "number"), ("label", "string"))
    if name != "string":
        assert copy_target(case.task.replace("十进制小数", "未知类型")) is None


@pytest.mark.parametrize("failure", ["missing", "extra", "wrong_type"])
def test_multi_field_copy_rejects_incomplete_or_mistyped_sources(failure: str) -> None:
    target = copy_target(copy_case("multi").task)
    assert target is not None
    data: JsonObject = {"tables": [], "records": [{"id": "record", "text": '{"balance":-0.1250,"label":"000783"}'}]}
    turn = CopyTurn(target, phase="edit", ref="receipt", data_json=json.dumps(data))
    sources: JsonObject = {"balance": {"pointer": "/balance"}, "label": {"pointer": "/label"}}
    if failure == "missing":
        del sources["label"]
    elif failure == "extra":
        sources["extra"] = {"pointer": "/label"}
    else:
        sources["label"] = {"pointer": "/balance"}
    with pytest.raises(ValueError):
        turn.lower({"sources": sources})


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("name", ["decimal", "precision", "string", "multi", "renamed"])
def test_production_copy_preserves_precision_and_writes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    field = "due" if name == "renamed" else "balance"
    source: JsonObject = {"pointer": "/record/label" if name == "string" else "/record/" + field}
    args: JsonObject = ({"sources": {"balance": source, "label": {"pointer": "/record/label"}}}
                        if name == "multi" else {"source": source})
    install_http(monkeypatch, [{"tool": "codemode", "args": {}}, {"tool": "codemode", "args": args},
                              {"tool": "done", "args": {"summary": "已按声明类型复制所选来源，保留其他字段。"}}])
    result = run_copy(tmp_path / "run", Target("http://scripted.test/v1", "fixture", 123456789, 10), name)
    assert result["passed"] is True, result
    assert result["successful_edits"] == result["source_captures"] == 1
    assert result["model_javascript_proposals"] == 0
