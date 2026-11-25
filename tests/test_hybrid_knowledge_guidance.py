"""Hybrid guidance reduces reference errors without changing citation eligibility."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.completion.contract import CHECK_FAIL, CompletionCheck
from codey.knowledge.answer_refs import KnowledgeReferenceError, answer_references
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference, canonical_json
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.chat_knowledge import read_knowledge_versions
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.research_completion_checks import knowledge_reference_checks, research_completion_feedback
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target
from tools.hybrid_knowledge_recovery_live_gate import preparation_replies

FAILED = [CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid")]


def session(*, required: bool = False, strict: bool = False) -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control", "knowledge.read", "knowledge.write"}),
        required_checks=("knowledge_versions",) if required else (), strict_research=strict), task_kind="hybrid")


def seed(store: KnowledgeStore) -> None:
    provenance: dict[str, str] = {"url": "https://example.test/other"}
    store.repository.commit("seed", [KnowledgeMutation(KnowledgeObject(
        "measured.json", "observation", "A recorded measurement.", "observed_result")),
        KnowledgeMutation(KnowledgeObject("other-source", "source", "Another source.", "source",
            provenance=provenance))])


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("read", ["none", "search", "failed", "uncommitted", "mismatched", "success"])
def test_hybrid_read_choices_do_not_close_legal_reference_space(tmp_path: Path, native: bool, read: str) -> None:
    store = KnowledgeStore(tmp_path)
    try:
        seed(store)
        task = session()
        delegate = ExecutionDelegate(session=task, research_tools=ResearchTools(None, store, None))
        if read != "none":
            call = ToolCall("knowledge_search" if read == "search" else "knowledge_read",
                           {"query": "measured"} if read == "search" else {
                               "id": "missing" if read == "failed" else "measured.json", "revision": 1}, "read")
            result = delegate.execute(call)[0]
            task._memory_results["receipt"] = result
            if read != "uncommitted":
                project_execution_record(task, "receipt", call, result, ok=result.ok)
            if read == "mismatched":
                task._memory_results["receipt"] = ToolResult(ToolCall(call.name, call.args, "other"),
                    result.model_text, ok=result.ok, canonical=result.canonical)
        snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(task, native=native)))
        spec = next(spec for spec in snapshot.model_specs if spec.name == "done")
        field = canonical_json_object(dict(spec.parameters)["knowledge_refs"])
        description = str(field["description"])
        expected: list[JsonObject] = [{"id": "measured.json", "revision": 1}] if read == "success" else []
        assert description.endswith("Committed structured-object reads: " + canonical_json(expected))
        assert "not automatic adoption" in description and "omit this optional field" in description
        assert "anyOf" not in decode_json_object(field["items"])
        for identity, revision in (("other-source", 1), ("measured.json", 1), ("measured.json", 99), ("s1", 1)):
            args: JsonObject = {"summary": "Observed measurement.", "knowledge_refs": [{"id": identity, "revision": revision}]}
            payload: JsonObject = {"tool": "done", "args": args}
            reply = (AssistantTurn(tool_calls=(ProviderToolCall("done", "done", args),)) if native else
                     decode_text_turn(json.dumps(payload)))
            assert not normalize_turn(reply, snapshot=snapshot).protocol_error
            task.last_done_args = args
            check, = knowledge_reference_checks(task, CompletionContext(knowledge_store=store))
            assert (check.status == "pass") is (identity != "s1" and revision == 1)
            assert task.last_done_args == args
    finally:
        store.close()


@pytest.mark.parametrize("required", [False, True])
def test_repair_reports_exact_rejected_identity_and_recorded_path(required: bool) -> None:
    task = session(required=required)
    task.record_edit("custom-output.json")
    task.last_done_args = {"knowledge_refs": [{"id": "custom-output.json", "revision": 3}]}
    assert knowledge_reference_checks(task)[0].status == "fail"
    feedback = research_completion_feedback(task, "", CompletionContext(), FAILED)
    assert '"id":"custom-output.json","revision":3' in feedback
    assert "missing or inaccessible" in feedback and "recorded output file path" in feedback
    assert "No successful knowledge_read" in feedback
    assert ("omit optional knowledge_refs" in feedback) is not required
    assert task.last_done_args == {"knowledge_refs": [{"id": "custom-output.json", "revision": 3}]}
    spec = next(spec for spec in build_turn_snapshot(task).model_specs if spec.name == "done")
    description = str(canonical_json_object(dict(spec.parameters)["knowledge_refs"])["description"])
    assert ("omit this optional field" in description) is not required


@pytest.mark.parametrize("case", ["valid-path-id", "wrong-version", "inaccessible", "old-answer", "unknown-path", "malformed"])
def test_feedback_uses_validation_not_filename_heuristics(tmp_path: Path, case: str) -> None:
    store = KnowledgeStore(tmp_path)
    try:
        seed(store)
        provenance: dict[str, str] = {"url": "https://example.test/private"}
        store.repository.commit("private", [KnowledgeMutation(KnowledgeObject(
            "restricted.json", "source", "Private source.", "source", scope="project", scope_id="other-project",
            provenance=provenance))])
        store.repository.commit("answer", [KnowledgeMutation(KnowledgeObject(
            "old-answer", "answer", "Old conclusion.", "model_inference"))])
        task = session()
        task.record_edit("measured.json")
        ref: JsonObject = {"id": "measured.json", "revision": 1}
        if case == "wrong-version":
            ref["revision"] = 99
        elif case == "inaccessible":
            ref["id"] = "restricted.json"
        elif case == "old-answer":
            ref["id"] = "old-answer"
        elif case == "unknown-path":
            ref["id"] = "unobserved.json"
        elif case == "malformed":
            ref["revision"] = True
        task.last_done_args = {"knowledge_refs": [ref]}
        context = CompletionContext(knowledge_store=store)
        check, = knowledge_reference_checks(task, context)
        assert (check.status == "pass") is (case == "valid-path-id")
        if case == "valid-path-id":
            assert answer_references([ref], store) == (KnowledgeReference("measured.json", 1, "uses"),)
            return
        feedback = research_completion_feedback(task, "", context, FAILED)
        assert "done.knowledge_refs rejected:" in feedback
        assert ("recorded output file path" in feedback) is (case == "wrong-version")
        assert ("original premises rather than an old answer" in feedback) is (case == "old-answer")
        assert task.last_done_args["knowledge_refs"] == [ref]
    finally:
        store.close()


def test_legacy_note_candidates_stay_legal_without_becoming_direct_reads(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    try:
        seed(store)
        note = KnowledgeNote.create(type="note", title="Legacy measurement", body="Measurement summary.")
        store.publish_note(note, "legacy", [KnowledgeMutation(KnowledgeObject(
            "linked-question", "question", "How repeatable is this measurement?", "model_inference"))])
        task = session()
        delegate = ExecutionDelegate(session=task, research_tools=ResearchTools(None, store, None))
        call = ToolCall("knowledge_read", {"id": note.id}, "legacy-read")
        result = delegate.execute(call)[0]
        assert result.ok and "linked-question" in result.model_text
        task._memory_results["legacy-read"] = result
        project_execution_record(task, "legacy-read", call, result, ok=result.ok)
        assert read_knowledge_versions(task.executed, task._memory_results) == []
        snapshot = build_turn_snapshot(task)
        field = canonical_json_object(dict(next(spec for spec in snapshot.model_specs if spec.name == "done").parameters)["knowledge_refs"])
        assert str(field["description"]).endswith("Committed structured-object reads: []")
        task.last_done_args = {"knowledge_refs": [{"id": "linked-question", "revision": 1}]}
        assert knowledge_reference_checks(task, CompletionContext(knowledge_store=store))[0].status == "pass"
    finally:
        store.close()


def test_saved_object_is_not_automatically_read_or_adopted(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    try:
        task = session()
        delegate = ExecutionDelegate(session=task, research_tools=ResearchTools(None, store, None))
        call = ToolCall("knowledge_commit", {"commit_id": "new-observation", "changes": [{"expected_revision": 0,
            "value": {"id": "just-saved", "kind": "observation", "content": "New measurement.", "origin": "observed_result"}}]}, "save")
        result = delegate.execute(call)[0]
        assert result.ok, result.model_text
        task._memory_results["saved"] = result
        project_execution_record(task, "saved", call, result, ok=result.ok)
        assert not read_knowledge_versions(task.executed, task._memory_results)
        assert not task.last_done_args
        task.last_done_args = {"knowledge_refs": [{"id": "just-saved", "revision": 1}]}
        assert knowledge_reference_checks(task, CompletionContext(knowledge_store=store))[0].status == "pass"
    finally:
        store.close()


def test_diagnostic_retains_exact_version_without_replacing_it(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path)
    try:
        seed(store)
        with pytest.raises(KnowledgeReferenceError) as rejected:
            answer_references([{"id": "measured.json", "revision": 99}], store)
        assert rejected.value.reference == KnowledgeReference("measured.json", 99, "uses")
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
def test_production_invalid_completion_repairs_without_replaying_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)

    def replies() -> Iterator[JsonObject]:
        yield from preparation_replies()
        wire = requests(directory)
        feedback = json.dumps(wire[-1]["messages"], ensure_ascii=False)
        assert "recorded output file path" in feedback and "omit optional knowledge_refs" in feedback
        assert "does not require repeating successful browser actions or file edits" in feedback
        yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851，文件已更新。"}}

    install_http(monkeypatch, replies(), native=native)
    result = gate.run_case(directory, "hybrid", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                           stale_edit=True, codemode_only=True)
    assert result["passed"] is True, result
    receipts = tool_receipts(directory / "state")
    for name, count in {"browser_navigate": 1, "browser_fill": 1, "browser_click": 1,
                        "browser_read": 1, "read_file": 2, "edit": 2}.items():
        assert sum(receipt.result.call.name == name for receipt in receipts) == count
    assert sum(receipt.result.call.name == "edit" and receipt.result.ok for receipt in receipts) == 1
