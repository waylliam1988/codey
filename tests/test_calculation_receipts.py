"""Exact calculations consume committed inputs and publish frozen source-bound results."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.answer_contract import AnswerScope, answer_scope, answer_spec, submit_answer
from codey.research.answer_report import AnswerReport, compile_answer_report
from codey.research.calculation import calculate, calculation_fields, read_data, render_calculations
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import source_observation
from codey.research.passages import ReadPassage
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tests.test_finding_choices import read_receipt


def session() -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), task_text="Calculate observed data")


def commit(task: TaskSession, ref: str, name: str, value: JsonObject) -> None:
    result = ToolResult(ToolCall(name, {}), "original result", ok=True, canonical={"value": value})
    task._memory_results[ref] = result
    project_execution_record(task, ref, result.call, result, ok=True)


def operand(ref: str, index: int, column: str = "value") -> JsonObject:
    return {"result_ref": ref, "pointer": f"/data/{index}/{column}"}


@pytest.mark.parametrize(("operation", "values", "unit", "expected", "output_unit"), [
    ("sum", ["0.1", "0.2"], "", "0.3", ""),
    ("subtract", ["31", "24"], "kg", "7", "kg"),
    ("growth_rate", ["24", "31"], "kg", "29.166667", "%"),
    ("percentage", ["24", "31"], "kg", "77.419355", "%"),
    ("percentage_points", ["24", "31"], "%", "7", "percentage points"),
    ("divide", ["31", "24"], "kg", "1.291667", ""),
    ("multiply", ["2", "3"], "", "6", ""),
    ("mean", ["24", "31"], "kg", "27.5", "kg"),
])
def test_exact_operations(operation: str, values: list[str], unit: str, expected: str, output_unit: str) -> None:
    task = session()
    rows: list[JsonValue] = [{"value": value, "unit": unit} for value in values]
    commit(task, "input", "read_data", {"format": "quantities", "data": rows, "passage_refs": ["p1"]})
    result = calculate({"operation": operation, "operands": [operand("input", i) for i in range(len(values))]}, task)
    assert (result["value"], result["unit"], result["passage_refs"]) == (expected, output_unit, ["p1"])
    assert result["validation"] == "execution_and_arithmetic; interpretation_unreviewed"


def test_units_weights_and_weight_provenance() -> None:
    task = session()
    commit(task, "values", "read_data", {"format": "quantities", "data": [
        {"value": "1", "unit": "kg"}, {"value": "500", "unit": "g"}], "passage_refs": ["p1"]})
    commit(task, "weights", "read_data", {"format": "json", "data": ["1", "3"], "passage_refs": ["p2"]})
    args: JsonObject = {"operation": "weighted_mean", "operands": [operand("values", 0), operand("values", 1)],
        "weights": [{"result_ref": "weights", "pointer": f"/data/{i}"} for i in range(2)]}
    result = calculate(args, task)
    assert result["value"] == "0.625" and result["unit"] == "kg" and result["passage_refs"] == ["p1", "p2"]
    assert calculate({"operation": "convert", "operands": [operand("values", 0)], "unit": "g"}, task)["value"] == "1000"
    with pytest.raises(ValueError, match="incompatible"):
        calculate({"operation": "convert", "operands": [operand("values", 0)], "unit": "m"}, task)
    with pytest.raises(ValueError, match="referenced weight"):
        calculate({**args, "weights": []}, task)


def test_missing_zero_denominator_and_forged_references() -> None:
    task = session()
    commit(task, "data", "read_data", {"format": "json", "data": ["3", "", "0", {"name": "selected"}], "passage_refs": []})
    refs: list[JsonValue] = [{"result_ref": "data", "pointer": f"/data/{i}"} for i in range(4)]
    with pytest.raises(ValueError, match="missing"):
        calculate({"operation": "sum", "operands": refs[:2]}, task)
    assert calculate({"operation": "sum", "operands": refs[:2], "missing": "skip"}, task)["value"] == "3"
    assert calculate({"operation": "count", "operands": refs, "missing": "skip"}, task)["value"] == "3"
    with pytest.raises(ArithmeticError):
        calculate({"operation": "divide", "operands": [refs[0], refs[2]]}, task)
    with pytest.raises(ValueError, match="committed"):
        calculate({"operation": "sum", "operands": [{"result_ref": "forged", "pointer": "/data/0"}]}, task)
    with pytest.raises(ValueError, match="metadata"):
        calculate({"operation": "sum", "operands": [{"result_ref": "data", "pointer": "/source_version"}]}, task)


def test_original_evidence_parsing_scope_and_report_slots() -> None:
    task = session()
    passage, original = read_receipt("https://example.org/input", "Observed baseline 24 kg and new value 31 kg.")
    task._memory_results["read"] = original
    project_execution_record(task, "read", original.call, original, ok=True)
    scope = answer_scope(task)
    data = read_data({"evidence": "e1", "format": "quantities"}, task, scope)
    assert data["passage_refs"] == [passage]
    commit(task, "parsed", "read_data", data)
    value = calculate({"operation": "growth_rate", "operands": [operand("parsed", 0), operand("parsed", 1)]}, task)
    commit(task, "computed", "calculate", value)
    frozen = answer_scope(task)
    assert frozen.schema_version == 9 and AnswerScope.from_payload(frozen.to_payload()) == frozen
    assert frozen.calculations == calculation_fields(task)
    spec = spec_for_tool("submit_research_answer")
    assert spec is not None
    description = answer_spec(spec, frozen).description
    assert '{{calc:c1}}' in description and '"operation":"growth_rate"' in description and '"result_ref"' not in description
    rendered = render_calculations("Growth: {{calc:c1}}.", frozen.calculations, frozen.aliases)
    assert rendered == "Growth: 29.166667 % [e1]."
    submitted = submit_answer(frozen, {"state": "answered", "markdown": "Growth: {{calc:c1}}."})
    assert "29.166667" in str(submitted)
    for slot in ("{{calc:c2}}", "{{calc:c0}}", "{{calc:computed}}", "{{calc:c1"):
        with pytest.raises(ValueError, match=r"calculation slot.*\{\{calc:c1\}\}"):
            render_calculations(slot, frozen.calculations, frozen.aliases)
    with pytest.raises(ValueError, match="outside"):
        render_calculations("{{calc:c1}}", frozen.calculations, ())


@pytest.mark.parametrize("mode", ["explicit", "table", "table_path"])
def test_codemode_filters_original_csv_and_calculates_through_kernel(tmp_path: Path, mode: str) -> None:
    (tmp_path / "input.csv").write_bytes(b"cohort,value,weight\nA,0.1,1\nB,999,1\nA,0.2,3\n")
    task = session()
    snapshot = build_turn_snapshot(task)
    assert restore_snapshot(snapshot_description(snapshot)) == snapshot
    read_data_spec = next(spec for spec in snapshot.frozen_specs if spec.name == "read_data")
    assert set(dict(read_data_spec.parameters)) == {"result_ref", "format", "path"}
    executor = build_execute_turn_executor(session=task, snapshot=snapshot, run_id="calculation", effect_scope="task",
        turn=0, project_path=tmp_path)
    read = "const source = await tools.read_file({path:'input.csv'});\n"
    file_selector = "path:'input.csv'" if mode == "table_path" else "result_ref:source.result_ref"
    read += "const parsed = await tools.read_data({" + file_selector + ", format:'csv'});\n"
    if mode != "explicit":
        code = read + """return unwrap(await tools.calculate({operation:'weighted_mean',table:{result_ref:parsed.result_ref,
    value_column:'value',weight_column:'weight',where:{column:'cohort',equals:'A'}}}));"""
    else:
        code = read + """
const rows = unwrap(parsed).data;
const indices = rows.flatMap((row, i) => row.cohort === 'A' ? [i] : []);
const references = column => indices.map(i => ({result_ref:parsed.result_ref,pointer:`/data/${i}/${column}`}));
return unwrap(await tools.calculate({operation:'weighted_mean',operands:references('value'),weights:references('weight')}));"""
    result = execute_turn(task, [ToolCall("codemode", {"code": code})], project_path=tmp_path,
        executors={"codemode": executor}, snapshot=snapshot, run_id="calculation", effect_scope="task", turn=0)[0]
    assert result.ok, result.model_text
    assert "0.175" in result.model_text
    assert [r.call.name for r in task._memory_results.values() if r.ok] == ["read_file", "read_data", "calculate", "codemode"]
    checked = next(r for r in task._memory_results.values() if r.call.name == "calculate")
    outcome = decode_json_object(checked.canonical["value"])
    inputs = outcome["inputs"]
    assert isinstance(inputs, list)
    assert [decode_json_object(ref)["pointer"] for ref in inputs] == ["/data/0/value", "/data/2/value"]
    if mode != "explicit":
        selection = decode_json_object(checked.call.args["table"])
        for column in ("missing", "value"):
            with pytest.raises(ValueError):
                calculate({"operation": "weighted_mean", "table": {**selection, "where": {"column": column, "equals": "A"}}}, task)


def test_archive_rechecks_calculation_receipt_and_preserves_original_source(tmp_path: Path) -> None:
    task = session()
    passage, original = read_receipt("https://example.org/fuel", "The same cohort used 24 kg then 31 kg.")
    task._memory_results["read"] = original
    project_execution_record(task, "read", original.call, original, ok=True)
    parsed = read_data({"evidence": "e1", "format": "quantities"}, task, answer_scope(task))
    commit(task, "data", "read_data", parsed)
    value = calculate({"operation": "growth_rate", "operands": [operand("data", 0), operand("data", 1)]}, task)
    commit(task, "calc", "calculate", value)
    frozen = answer_scope(task)
    args: JsonObject = {"state": "answered", "markdown": "Same-cohort growth using the first measurement as denominator: {{calc:c1}}."}
    submitted = ToolResult(ToolCall("submit_research_answer", args), "recorded", ok=True,
        canonical={"research_submission": submit_answer(frozen, args)})
    task._memory_results["submit"] = submitted
    project_execution_record(task, "submit", submitted.call, submitted, ok=True)
    source, body, _ = source_observation(original.canonical["research_observation"])
    ledger = ResearchLedger()
    ledger.read_passages[passage] = ReadPassage(passage, source, body, (), 0, len(body))
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        report = compile_answer_report(task, ledger, store, project="", session_id="calculation")
        assert "29.166667 % [e1]" in report.markdown
        assert AnswerReport.from_payload(report.to_payload()) == report
        assert report.evidence_bindings[0]["excerpt"] == body
        forged = replace(frozen, calculations=({**frozen.calculations[0], "value": "99"},))
        task._memory_results["submit"] = replace(submitted, canonical={"research_submission": submit_answer(forged, args)})
        with pytest.raises(ValueError, match="original receipts"):
            compile_answer_report(task, ledger, store, project="", session_id="calculation")
    finally:
        store.close()


@pytest.mark.parametrize(("second_value", "expected"), [("0.4", "0.325"), ("0.2", None)])
def test_codemode_assertion_checks_a_changed_calculation_input(
    tmp_path: Path, second_value: str, expected: str | None,
) -> None:
    table = "cohort,value,weight\nA,0.1,1\nB,999,1\nA,0.2,3\n"
    (tmp_path / "original.csv").write_text(table, encoding="utf-8")
    (tmp_path / "changed.csv").write_text(table.replace("A,0.2,3", f"A,{second_value},3"), encoding="utf-8")
    task = session()
    snapshot = build_turn_snapshot(task)
    executor = build_execute_turn_executor(session=task, snapshot=snapshot, run_id="sensitivity", effect_scope="task",
        turn=0, project_path=tmp_path)
    code = """
async function mean(path) {
    const file = await tools.read_file({path});
    const table = await tools.read_data({result_ref:file.result_ref, format:'csv'});
    const proof = unwrap(await tools.calculate({operation:'weighted_mean', table:{
        result_ref:table.result_ref, value_column:'value', weight_column:'weight', where:{column:'cohort',equals:'A'}
    }}));
    if (proof.inputs.length !== 2) throw new Error('wrong selection');
    return proof.value;
}
const baseline = await mean('original.csv');
const changed = await mean('changed.csv');
if (!(Number(changed) > Number(baseline))) throw new Error('weighted mean did not increase');
return {baseline, changed};
"""
    result, = execute_turn(task, [ToolCall("codemode", {"code": code})], project_path=tmp_path,
        executors={"codemode": executor}, snapshot=snapshot, run_id="sensitivity", effect_scope="task", turn=0)
    assert result.ok is (expected is not None), result.model_text
    if expected is None:
        assert "weighted mean did not increase" in result.model_text
    else:
        assert result.canonical["value"] == {"baseline": "0.175", "changed": expected}


@pytest.mark.parametrize("mode", ["inline_binding", "named_receipt", "inline_access"])
def test_codemode_lowers_csv_rows_and_receipt_access_before_dispatch(tmp_path: Path, mode: str) -> None:
    csv = "cohort,value\nA,0.1\nB,999\n"
    (tmp_path / "table.csv").write_bytes(csv.encode())
    task = session()
    snapshot = build_turn_snapshot(task, native=True)
    read = "await tools.read_file({path:inputs.path})"
    file_code = (f"const file = unwrap({read});" if mode == "inline_binding"
                 else f"const receipt = {read}; const file = unwrap(receipt);")
    ref = "unwrap(receipt).result_ref" if mode == "inline_access" else "alias.result_ref"
    code = file_code + """
const alias = file;
const table = await tools.read_data({result_ref:ORIGINAL_REF, format:inputs.format});
return {raw:file.text, rows:table.rows.filter(row => row.cohort === 'A')};
"""
    code = code.replace("ORIGINAL_REF", ref)
    reply = AssistantTurn(tool_calls=(ProviderToolCall("program", "codemode", {
        "code": code, "inputs": {"path": "table.csv", "format": "csv"},
    }),))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.protocol_error and plan.arg_repair_counts["codemode_source_shape"] == 2
    call, = plan.calls
    assert call.call_id == "program" and call.args["code"] != code
    executor = build_execute_turn_executor(session=task, snapshot=snapshot, run_id="normalize", effect_scope="task",
        turn=0, project_path=tmp_path)
    result, = execute_turn(task, plan.calls, project_path=tmp_path, executors={"codemode": executor},
        snapshot=snapshot, run_id="normalize", effect_scope="task", turn=0)
    assert result.ok, result.model_text
    assert result.canonical["value"] == {"raw": csv, "rows": [{"cohort": "A", "value": "0.1"}]}
    records = {ref: row for ref, row in task._memory_results.items() if row.call.name != "codemode"}
    assert [row.call.name for row in records.values()] == ["read_file", "read_data"]
    file_ref, table_ref = records
    assert records[table_ref].call.args["result_ref"] == file_ref


@pytest.mark.parametrize("observed", [False, True])
def test_codemode_read_data_keeps_the_original_file_receipt_or_failure(tmp_path: Path, observed: bool) -> None:
    (tmp_path / "input.csv").write_bytes(b"cohort,value,weight\nA,0.1,1\nA,0.2,3\n")
    task = session()
    run_id = "observed-table"
    if observed:
        snapshot = build_turn_snapshot(task)
        reader = build_execute_turn_executor(session=task, snapshot=snapshot, run_id=run_id, effect_scope="task",
            turn=0, project_path=tmp_path)
        opened, = execute_turn(task, [ToolCall("codemode", {"code": (
            "return unwrap(await tools.read_file({path:'input.csv'}));"
        )})], project_path=tmp_path, executors={"codemode": reader}, snapshot=snapshot,
            run_id=run_id, effect_scope="task", turn=0)
        assert opened.ok, opened.model_text
    snapshot = build_turn_snapshot(task)
    executor = build_execute_turn_executor(session=task, snapshot=snapshot, run_id=run_id, effect_scope="task",
        turn=1, project_path=tmp_path)
    code = """
const table = await tools.read_data({path:'input.csv',format:'csv'});
return unwrap(await tools.calculate({operation:'weighted_mean',table:{
    result_ref:table.result_ref,value_column:'value',weight_column:'weight',where:{column:'cohort',equals:'A'}
}}));
"""
    if not observed:
        code = "await tools.read_data({path:'another.csv',format:'csv'});\n" + code
    result, = execute_turn(task, [ToolCall("codemode", {"code": code})], project_path=tmp_path,
        executors={"codemode": executor}, snapshot=snapshot, run_id=run_id, effect_scope="task", turn=1)
    assert result.ok is observed, result.model_text
    receipts = task._memory_results
    file_reads = [ref for ref, row in receipts.items() if row.call.name == "read_file"]
    _, parsed = next((ref, row) for ref, row in receipts.items() if row.call.name == "read_data")
    if observed:
        file_ref, = file_reads
        assert decode_json_object(parsed.canonical["value"])["source_ref"] == file_ref
        assert decode_json_object(result.canonical["value"])["value"] == "0.175"
    else:
        assert not file_reads and not parsed.ok
        assert "read the selected file in this task before parsing its data" in parsed.model_text
        assert result.model_text.count(parsed.model_text) == 1
        assert all(row.call.name != "calculate" for row in receipts.values())


@pytest.mark.parametrize(("text", "format_name", "expected"), [
    ('{"value":0.1000000000000000001}', "json", {"value": "0.1000000000000000001"}),
    ("value,weight\n0.1,2\n0.2,3\n", "csv", [{"value": "0.1", "weight": "2"}, {"value": "0.2", "weight": "3"}]),
])
def test_table_parsing_uses_original_complete_file_receipt(tmp_path: Path, text: str, format_name: str, expected: JsonValue) -> None:
    (tmp_path / "input.txt").write_text(text, encoding="utf-8")
    task = session()
    snapshot = build_turn_snapshot(task)
    executor = build_execute_turn_executor(session=task, snapshot=snapshot, run_id="table", effect_scope="task", turn=0,
        project_path=tmp_path)
    result = execute_turn(task, [ToolCall("codemode", {"code": "return unwrap(await tools.read_file({path:'input.txt'}));"})],
        project_path=tmp_path, executors={"codemode": executor}, snapshot=snapshot, run_id="table", effect_scope="task", turn=0)[0]
    assert result.ok, result.model_text
    reference = next(ref for ref, row in task._memory_results.items() if row.call.name == "read_file")
    (tmp_path / "input.txt").write_text("external replacement", encoding="utf-8")
    parsed = read_data({"result_ref": reference, "format": format_name}, task, None)
    assert parsed["data"] == expected and parsed["source_ref"] == reference
    original = task._memory_results[reference]
    task._memory_results[reference] = replace(original, truncated=True)
    with pytest.raises(ValueError, match="complete"):
        read_data({"result_ref": reference, "format": format_name}, task, None)


@pytest.mark.parametrize(("value", "scale", "expected"), [("0.125", 2, "0.12"), ("0.135", 2, "0.14"), ("-0.125", 2, "-0.12")])
def test_decimal_half_even_rounding_is_explicit(value: str, scale: int, expected: str) -> None:
    task = session()
    commit(task, "data", "read_data", {"format": "json", "data": [value], "passage_refs": []})
    result = calculate({"operation": "sum", "operands": [{"result_ref": "data", "pointer": "/data/0"}], "scale": scale}, task)
    assert result["value"] == expected and result["rounding"] == "half_even"
