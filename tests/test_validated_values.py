"""Construction, immutability and wire boundaries for validated internal values."""
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from operator import setitem

import pytest

from codey.research.answer_contract import digest
from codey.research.answer_report import AnswerReport, render_answer
from codey.research.knowledge_updates import compile_updates, normalize_updates
from codey.research.report_snapshot import (
    ReportCitation,
    ReportEvidence,
    ReportKnowledgeBatch,
    ReportMutation,
    ReportReference,
    ReportSource,
)
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationTransitionError
from codey.runtime.core.progress import ProgressSnapshot, ToolAttemptRecord
from codey.types.json_value import JsonObject, canonical_json_object


def report_with_evidence() -> AnswerReport:
    markdown = "Observation [e1]."
    source = ReportSource({"id": "source", "revision": 1, "title": "Source", "url": "https://example.com/",
        "content_hash": "original", "retrieved_at": "now", "quality": {"kind": "primary"}})
    evidence = ReportEvidence({"label": "e1", "evidence_id": "evidence", "evidence_revision": 1,
        "source_id": "source", "source_revision": 1, "excerpt": "Observation", "excerpt_digest": digest("Observation"),
        "page": None, "locator": "passage", "relation": "uses", "passage_ref": "original", "start": 0, "end": 11})
    citation = ReportCitation({"label": "e1", "start": markdown.index("[e1]"),
                               "end": markdown.index("[e1]") + 4, "passage_ref": "original"})
    return AnswerReport(render_answer(markdown, (source,), (evidence,)), markdown, "answered", digest("Task"),
        "submission", (citation,), (source,), (evidence,), (ReportReference({"id": "evidence", "revision": 1}),),
        (), ("read",), ("submitted",), ("Task",))


@pytest.mark.parametrize("changes", [
    {"questions": ("Another task",)}, {"citation_bindings": ()}, {"text": "Changed rendering"},
    {"knowledge_refs": ()}, {"attempt_refs": ()}, {"selection_receipts": ()}, {"state": "invented"},
])
def test_report_rejects_invalid_replacement_at_construction(changes: Mapping[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(report_with_evidence(), **changes)


def test_report_records_detach_the_input_and_freeze_nested_containers() -> None:
    source: JsonObject = {"id": "source", "revision": 1, "title": "Source", "url": "https://example.com/",
                          "content_hash": "original", "retrieved_at": "now", "quality": {"kind": "primary"}}
    record = ReportSource(source)
    quality = source["quality"]
    assert isinstance(quality, dict)
    quality["kind"] = "changed"
    assert record["quality"]["kind"] == "primary"
    with pytest.raises(TypeError):
        setitem(record["quality"], "kind", "changed")
    with pytest.raises(FrozenInstanceError):
        record.__setattr__("_values", {})


def test_mutating_exported_report_payload_does_not_change_the_report() -> None:
    report = report_with_evidence()
    expected = report.to_payload()
    exported = report.to_payload()
    sources = exported["source_versions"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    quality = sources[0]["quality"]
    assert isinstance(quality, dict)
    quality["kind"] = "changed"
    assert report.to_payload() == expected
    assert AnswerReport.from_payload(expected) == report


def test_report_freezes_the_authored_batch_and_mutations() -> None:
    report = report_with_evidence()
    updates = normalize_updates([{"key": "k1", "kind": "claim", "content": "A supported conclusion.",
                                  "references": ["e1"]}], {"e1"})
    mutations = compile_updates(updates, report.evidence_bindings, scope="personal", owner="owner",
        namespace=digest([report.proposal_digest, report.selection_receipts, "session"]),
        submission_digest=report.proposal_digest)
    batch = ReportKnowledgeBatch({"updates": updates, "scope": "personal", "scope_id": "owner", "session_id": "session"})
    report = replace(report, knowledge_batch=batch, knowledge_mutations=tuple(ReportMutation(row) for row in mutations))
    expected = report.to_payload()
    updates[0]["content"] = "Changed authored conclusion."
    assert report.to_payload() == expected
    rows = batch["updates"]
    assert isinstance(rows, tuple)
    entries: Sequence[object] = rows
    assert isinstance(entries[0], Mapping)
    aliases: object = entries[0]["aliases"]
    assert isinstance(aliases, tuple)
    mutation = report.knowledge_mutations[0]["value"]
    assert isinstance(mutation["references"], tuple)
    assert AnswerReport.from_payload(expected) == report


@pytest.mark.parametrize("changes", [{"unknown": True}, {"state": "invented"}, {"compilation_digest": "corrupt"}])
def test_report_decoder_keeps_rejecting_corrupt_wire(changes: Mapping[str, object]) -> None:
    payload = {**report_with_evidence().to_payload(), **changes}
    with pytest.raises(ValueError):
        AnswerReport.from_payload(payload)


@pytest.mark.parametrize("changes", [
    {"limit": 0}, {"limit": True}, {"idle": -1}, {"schema_version": 5}, {"last_batch_id": "x" * 241},
    {"repair_failure_key": "failure"}, {"repair_rejections": 1},
    {"schema_version": 1, "repair_failure_key": "failure", "repair_rejections": 1},
    {"schema_version": 2, "warned_check_cycle": "cycle", "warning_batch_id": "batch"},
    {"schema_version": 3, "recovery_progress_turn": 1, "last_batch_id": "batch"},
    {"schema_version": 4, "recovery_progress_turn": 1},
    {"schema_version": 3, "warned_check_cycle": "cycle", "warning_batch_id": "batch",
     "last_batch_id": "batch", "check_cycle_repeated": True},
    {"seen": (("run", "read", "bad"),)},
    {"seen": (("run", "read", "a" * 24),) * 2},
    {"seen": tuple(("run", str(i), "a" * 24) for i in range(257))},
    {"attempts": (ToolAttemptRecord("read", "call", "result", True, False, 0),) * 17},
])
def test_progress_rejects_invalid_replacement(changes: Mapping[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(ProgressSnapshot(), **changes)


@pytest.mark.parametrize("changes", [{"turn": -1}, {"edit_epoch": -1}, {"turn": True}, {"ok": 1}, {"changed": 0}])
def test_attempt_rejects_invalid_replacement(changes: Mapping[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(ToolAttemptRecord("read", "call", "result", True, False, 0), **changes)


@pytest.mark.parametrize("changes", [
    {"invalid_turns": -1}, {"invalid_turns": True}, {"closing_round": 5}, {"closing_round": True},
    {"length_continuations": (-1,)}, {"length_continuations": (1, 1)},
    {"length_continuations": (2, 1)}, {"length_continuations": (0, 1, 2, 3)},
])
def test_counters_reject_invalid_replacement(changes: Mapping[str, object]) -> None:
    with pytest.raises(RuntimeOperationTransitionError):
        replace(RuntimeExecutionCounters(), **changes)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_progress_wire_roundtrips_all_supported_versions(version: int) -> None:
    snapshot = ProgressSnapshot(seen=(("run", "read", "a" * 24),), schema_version=version)
    assert ProgressSnapshot.from_payload(snapshot.to_payload()) == snapshot
    counters = RuntimeExecutionCounters(progress=snapshot)
    assert RuntimeExecutionCounters.from_payload(counters.to_payload()) == counters


def test_legacy_counter_wire_is_still_decoded() -> None:
    payload: dict[str, object] = {"invalid_turns": 0, "length_continuation_used": True,
                                  "closing_round": 0, "progress": ProgressSnapshot().to_payload()}
    counters = RuntimeExecutionCounters.from_payload(payload)
    assert counters.length_continuations == (0,)
    assert "length_continuation_used" not in counters.to_payload()


def test_report_record_wire_remains_a_plain_json_object() -> None:
    report = report_with_evidence()
    payload = report.to_payload()
    assert canonical_json_object(payload) == payload
    assert payload["schema"] == "research_answer_v1"
