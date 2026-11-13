"""Experimental selection over real persisted Chromium receipts; no production patch."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, decode_json_value
from tools.completion_artifact_audit import EffectReceipt
from tools.completion_explanation_ab import Target
from tools.receipt_fact_delivery import Evidence, declare_needs, deliver, evidence_from_receipts
from tools.receipt_fact_delivery_ab import acquire, cases, score

pytestmark = pytest.mark.usefixtures(
    "public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


@dataclass(frozen=True)
class Captured:
    receipts: list[EffectReceipt]
    evidence: dict[str, Evidence]


_CAPTURED: dict[str, Captured] = {}


def capture(tmp_path: Path) -> dict[str, Captured]:
    if not _CAPTURED:
        for page, _expected in cases():
            receipts = acquire(tmp_path / page.name, page, Target("http://scripted.test/v1", "scripted-fixture", 7, 10))
            _CAPTURED[page.name] = Captured(receipts, evidence_from_receipts(receipts))
    return _CAPTURED


def plan(case: str) -> JsonObject:
    subject = {"food": "福州鱼丸", "orders": "A-173", "inventory": "西仓", "ambiguous": "A-173"}[case]
    labels = {"food": ("净重", "校验码"), "orders": ("送达时间", "追踪号"),
              "inventory": ("可用库存",), "ambiguous": ("送达时间", "追踪号")}[case]
    return {"needs": [{"key": f"f{index + 1}", "label": label, "subject": subject}
                      for index, label in enumerate(labels)]}


def selected(case: str) -> JsonObject:
    document = case == "inventory"
    labels = {"food": ("净重", "校验码"), "orders": ("送达时间", "追踪号"),
              "inventory": ("可用库存",), "ambiguous": ("送达时间", "追踪号")}[case]
    return {"explanation": "已查阅指定记录。", "bindings": [
        {"fact": f"f{index + 1}", "evidence": "e1", "records": "/documents/0/warehouses" if document else "/tables/0/rows",
         "identity": "/仓库" if document else "/样本" if case == "food" else "/订单号", "value": "/" + label}
        for index, label in enumerate(labels)]}


@pytest.mark.parametrize("case", ["food", "orders", "inventory", "ambiguous"])
def test_omitted_prose_is_completed_from_selected_receipts(tmp_path: Path, case: str) -> None:
    captured = capture(tmp_path)
    needs = declare_needs(plan(case))
    reply = selected(case)
    output = deliver(needs, reply, captured[case].evidence)
    expected = next(expected for page, expected in cases() if page.name == case)
    assert score(needs, output, expected)["passed"] is True, output
    assert output["complete"] is (case != "ambiguous")
    facts = decode_json_object(output["facts"])
    if case == "orders":
        assert facts == {"f1": "2026-11-18", "f2": "0042091"}
    elif case == "inventory":
        assert facts == {"f1": 0} and type(facts["f1"]) is int
    elif case == "food":
        assert facts == {"f1": "173 克", "f2": "QUARTZ_851"}
    else:
        assert facts == {"f2": "0042091"}
        missing = output["missing"]
        assert isinstance(missing, list) and decode_json_object(missing[0])["label"] == "送达时间"
    baseline: JsonObject = {"text": reply["explanation"], "missing": []}
    assert score(needs, baseline, expected, arm="A")["passed"] is False
    proofs = decode_json_object(output["provenance"])
    source = captured[case].evidence["e1"]
    assert proofs and all(decode_json_object(proof)["receipt_ref"] == source.receipt for proof in proofs.values())
    assert all(decode_json_object(proof)["read_ref"] == source.read_receipt for proof in proofs.values())


@pytest.mark.parametrize("defect", ["failed", "truncated", "model_eval", "read_missing", "read_truncated", "reloaded"])
def test_unverified_receipts_cannot_fill_facts(tmp_path: Path, defect: str) -> None:
    captured = capture(tmp_path)
    original = captured["orders"]
    modified: list[EffectReceipt] = []
    for receipt in original.receipts:
        result = receipt.result
        if receipt.identity == original.evidence["e1"].receipt:
            if defect == "failed":
                result = replace(result, ok=False)
            elif defect == "truncated":
                value = decode_json_object(result.canonical["value"])
                result = replace(result, canonical={**result.canonical, "value": {**value, "truncated": True}})
            elif defect == "model_eval":
                result = replace(result, call=ToolCall("browser_eval", {"code": "return {invented:173};"}))
        if receipt.identity == original.evidence["e1"].read_receipt:
            if defect == "read_missing":
                continue
            if defect == "read_truncated":
                result = replace(result, truncated=True)
        modified.append(replace(receipt, result=result))
    if defect == "reloaded":
        source = original.evidence["e1"]
        modified.append(EffectReceipt("later-navigation", ToolResult(call=ToolCall("browser_navigate", {"url": source.url}),
            ok=True, model_text="Reloaded", audit={"browser_document": {"url": source.url, "loader_id": "other-loader"}}), None))
    evidence = evidence_from_receipts(modified)
    assert evidence == {}
    output = deliver(declare_needs(plan("orders")), selected("orders"), evidence)
    assert output["facts"] == {} and output["complete"] is False


@pytest.mark.parametrize("records", ["/tables/0/rows/0", "/tables/0/rows/1", "/documents/0/unowned_dates"])
def test_ambiguous_dates_cannot_be_taken_from_another_record(tmp_path: Path, records: str) -> None:
    captured = capture(tmp_path)
    reply = selected("ambiguous")
    bindings = reply["bindings"]
    assert isinstance(bindings, list)
    bindings[0] = {**decode_json_object(bindings[0]), "records": records}
    output = deliver(declare_needs(plan("ambiguous")), reply, captured["ambiguous"].evidence)
    assert output["facts"] == {"f2": "0042091"} and output["complete"] is False
    missing = output["missing"]
    assert isinstance(missing, list) and decode_json_object(missing[0])["subject"] == "A-173"


def test_duplicate_owner_is_ambiguous_even_when_values_agree(tmp_path: Path) -> None:
    captured = capture(tmp_path)
    source = captured["orders"].evidence["e1"]
    data = decode_json_object(source.data)
    tables = data["tables"]
    assert isinstance(tables, list)
    rows = decode_json_object(tables[0])["rows"]
    assert isinstance(rows, list)
    # Use the exact same values to ensure equality is not used to hide ambiguity.
    rows.append(decode_json_value(rows[1]))
    tables[0] = {**decode_json_object(tables[0]), "rows": rows}
    output = deliver(declare_needs(plan("orders")), selected("orders"), {"e1": replace(source, data=data)})
    assert output["facts"] == {} and output["complete"] is False


def test_same_selectors_work_when_source_values_change(tmp_path: Path) -> None:
    captured = capture(tmp_path)
    source = captured["food"].evidence["e1"]
    data = decode_json_object(source.data)
    tables = data["tables"]
    assert isinstance(tables, list)
    rows = decode_json_object(tables[0])["rows"]
    assert isinstance(rows, list)
    rows[1] = {**decode_json_object(rows[1]), "净重": "229 克", "校验码": "NEW_0042"}
    tables[0] = {**decode_json_object(tables[0]), "rows": rows}
    changed = replace(source, data=data, text=source.text.replace("173 克", "229 克").replace("QUARTZ_851", "NEW_0042"))
    output = deliver(declare_needs(plan("food")), selected("food"), {"e1": changed})
    assert output["facts"] == {"f1": "229 克", "f2": "NEW_0042"}


@pytest.mark.parametrize("defect", ["copied_value", "extra_subject", "duplicate_binding", "unknown_fact", "redeclare"])
def test_model_cannot_replace_selection_with_a_copied_value(tmp_path: Path, defect: str) -> None:
    captured = capture(tmp_path)
    reply = selected("orders")
    bindings = reply["bindings"]
    assert isinstance(bindings, list)
    first = decode_json_object(bindings[0])
    if defect == "copied_value":
        bindings[0] = {**first, "literal": "2026-11-16"}
    elif defect == "extra_subject":
        bindings[0] = {**first, "subject": "B-264"}
    elif defect == "duplicate_binding":
        bindings.append(first)
    elif defect == "unknown_fact":
        bindings[0] = {**first, "fact": "unrequested"}
    else:
        reply["needs"] = list[JsonValue]()
    if defect in {"unknown_fact", "redeclare"}:
        with pytest.raises(ValueError):
            deliver(declare_needs(plan("orders")), reply, captured["orders"].evidence)
    else:
        output = deliver(declare_needs(plan("orders")), reply, captured["orders"].evidence)
        assert output["facts"] == {"f2": "0042091"} and output["complete"] is False


def test_correct_prose_is_scored_independently_of_binding_subject() -> None:
    needs = declare_needs({"needs": [
        {"key": "weight", "label": "净重", "subject": "福州鱼丸的净重"},
        {"key": "code", "label": "校验码", "subject": "福州鱼丸的校验码"}]})
    output: JsonObject = {"text": "福州鱼丸的净重是173 克，校验码是QUARTZ_851。", "missing": []}
    expected = cases()[0][1]
    result = score(needs, output, expected, arm="A")
    assert result["passed"] is True and result["declaration_usable"] is False


@pytest.mark.parametrize("facts", [{}, {"f1": "0"}, {"f1": False}])
def test_diagnostic_zero_is_not_a_delivered_inventory_value(facts: JsonObject) -> None:
    output: JsonObject = {"text": "Expected one record owned by 西仓; found 0.", "missing": [], "facts": facts}
    assert score(declare_needs(plan("inventory")), output, cases()[2][1])["passed"] is False


def test_prose_inventory_zero_must_be_a_complete_number() -> None:
    needs = declare_needs(plan("inventory"))
    expected = cases()[2][1]
    assert score(needs, {"text": "西仓的可用库存是0。", "missing": []}, expected, arm="A")["passed"] is True
    assert score(needs, {"text": "西仓的可用库存是10。", "missing": []}, expected, arm="A")["passed"] is False
