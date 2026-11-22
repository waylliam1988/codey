"""Observed choice menus keep finding inputs on original source identities."""
from __future__ import annotations

import hashlib
from dataclasses import replace

from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.research.answer_contract import AnswerScope, digest
from codey.research.finding_choices import finding_choices
from codey.research.ledger import OpenedSource
from codey.research.passages import passage_ref
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object


def read_receipt(url: str, text: str, source_id: str = "s1") -> tuple[str, ToolResult]:
    text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    source = OpenedSource(url, url, "Study", "2026-11-22T10:00:00+08:00", text_hash)
    ref = passage_ref(url, text_hash, 0, len(text))
    result = ToolResult(ToolCall("open_url", {"url": url}), text, ok=True, canonical={
        "read_passages": [{"ref": ref, "start": 0, "end": len(text)}],
        "research_observation": {"source": source.to_dict(), "text": text, "pages": []}, "source_id": source_id})
    return ref, result


def test_finding_choices_preserve_canonical_ids_without_alias_repair() -> None:
    ref, result = read_receipt("https://example.org/study?sample=A%2FB#data", "Measured input was 24 kg.")
    spec = spec_for_tool("save_finding")
    assert spec is not None
    scope = AnswerScope(digest("task"), (("e1", ref),), ("read",))
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                                     strict_research=True), task_kind="research", task_text="Compare the measured inputs.")
    session._memory_results["read"] = result
    chosen = finding_choices(spec, scope, session)
    assert chosen is not None
    properties = dict(chosen.parameters)
    assert canonical_json_object(properties["passage_ref"])["enum"] == [ref]
    assert canonical_json_object(properties["source_ref"])["enum"] == ["s1"]
    assert "enum" not in decode_json_object(dict(spec.parameters)["passage_ref"]), "mutated the registry contract"
    good: JsonObject = {"claim": "Measured input was 24 kg.", "passage_ref": ref}
    assert not validate_args_with_spec(chosen, good)
    assert not validate_args_with_spec(chosen, {**good, "source_ref": "s1"})
    for invented in ("p1", "e1", f"[{ref}]"):
        assert validate_args_with_spec(chosen, {**good, "passage_ref": invented})
    assert validate_args_with_spec(chosen, {**good, "source_ref": "https://example.org/invented"})
    session.record_open(str(result.call.args["url"]), source_id="s1")
    snapshot = build_turn_snapshot(session)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    offered = next(row for row in restored.frozen_specs if row.name == "save_finding")
    assert canonical_json_object(dict(offered.parameters)["source_ref"])["enum"] == ["s1"]
    lowered, problem = ExecutionDelegate(session=session)._lower_finding_source({**good, "source_ref": "s1"})
    assert not problem and lowered["source_ref"] == result.call.args["url"]


def test_failed_and_unselected_read_receipts_cannot_extend_the_menu() -> None:
    ref, result = read_receipt("https://example.org/selected", "Measured input was 24 kg.")
    other, unselected = read_receipt("https://example.org/unselected", "Measured input was 31 kg.", "s2")
    spec = spec_for_tool("save_finding")
    assert spec is not None
    scope = AnswerScope(digest("task"), (("e1", ref),), ("read",))
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    session._memory_results.update({"read": result, "failed": replace(unselected, ok=False), "other": unselected})
    chosen = finding_choices(spec, scope, session)
    assert chosen is not None
    properties = dict(chosen.parameters)
    assert canonical_json_object(properties["passage_ref"])["enum"] == [ref]
    assert other not in str(properties["source_ref"])
    assert canonical_json_object(properties["source_ref"])["enum"] == ["s1"]


def test_without_read_evidence_the_original_contract_is_preserved() -> None:
    spec = spec_for_tool("save_finding")
    assert spec is not None
    assert finding_choices(spec, AnswerScope(digest("task"), (), ()), TaskSession(TaskPolicy(frozenset({"control"})))) == spec


def test_declared_comparison_choices_use_only_unmet_original_predicates() -> None:
    ref, read = read_receipt("https://example.org/study", "Measured input was 24 kg.")
    values: list[JsonObject] = [{"subject": "Atlas", "factor": "method A", "comparator": "method B",
        "endpoint": "fuel consumption", "direction": direction, "uncertainty": "unknown"}
        for direction in ("lower", "higher")]
    checks = tuple(require_tool_success("save_finding", arguments={"comparison": value}) for value in values)
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=checks))
    session._memory_results["read"] = read
    scope = AnswerScope(digest("task"), (("e1", ref),), ("read",))
    spec = spec_for_tool("save_finding")
    assert spec is not None and "comparison" not in spec.required
    chosen = finding_choices(spec, scope, session)
    assert chosen is not None
    assert "comparison" in chosen.required
    assert canonical_json_object(dict(chosen.parameters)["comparison"])["enum"] == values
    call = ToolCall("save_finding", {"claim": "Measured input was 24 kg.", "passage_ref": ref, "comparison": values[0]})
    result = ToolResult(call, "saved", ok=True)
    session._memory_results["finding"] = result
    project_execution_record(session, "finding", call, result, ok=True)
    next_choice = finding_choices(spec, scope, session)
    assert next_choice is not None
    assert canonical_json_object(dict(next_choice.parameters)["comparison"])["enum"] == [values[1]]
    assert validate_args_with_spec(next_choice, call.args), "completed predicate remained selectable"


def test_completed_comparison_stage_closes_and_only_new_evidence_reopens() -> None:
    from codey.research.finding_stage import finding_stage_error

    comparison: JsonObject = {"subject": "Atlas", "factor": "A", "comparator": "B", "endpoint": "fuel",
                              "direction": "lower", "uncertainty": "unknown"}
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(
        require_tool_success("save_finding", arguments={"comparison": comparison}),)))
    ref, read = read_receipt("https://example.org/first", "Measured input was 24 kg.")

    def commit(identity: str, result: ToolResult) -> None:
        session._memory_results[identity] = result
        project_execution_record(session, identity, result.call, result, ok=result.ok)

    commit("read", read)
    args: JsonObject = {"claim": "Measured input was 24 kg.", "passage_ref": ref, "comparison": comparison}
    commit("saved", ToolResult(ToolCall("save_finding", args), "saved", ok=True))
    spec = spec_for_tool("save_finding")
    assert spec is not None
    scope = AnswerScope(digest("task"), (("e1", ref),), ("read",))
    assert finding_choices(spec, scope, session) is None
    assert finding_stage_error(session, args) == "", "exact committed retry must retain idempotence"
    assert "finding_stage_complete" in finding_stage_error(session, {"claim": "again", "passage_ref": ref})
    commit("repeated-read", read)
    assert finding_choices(spec, scope, session) is None
    narrowed = replace(read, canonical={**read.canonical, "read_passages": [{"ref": "narrowed", "start": 1, "end": 10}]})
    commit("narrowed-read", narrowed)
    assert finding_choices(spec, scope, session) is None, "a subrange of an already read source is not fresh evidence"
    _, fresh = read_receipt("https://example.org/new", "Measured input was 31 kg.", "s2")
    commit("fresh", fresh)
    offered = finding_choices(spec, scope, session)
    assert offered is not None and "comparison" in offered.required
    assert "finding_stage_arguments" in finding_stage_error(session, {"claim": "missing comparison", "passage_ref": ref})
