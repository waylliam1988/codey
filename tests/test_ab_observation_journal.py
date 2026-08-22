from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.manual.ab_journal import (
    ABJournalIdentityMismatch,
    ABJournalReader,
    ABJournalWriter,
    journal_directory_for,
)


def _writer(directory: Path, *, run_id: str = "run-1", provider: str = "deepseek") -> ABJournalWriter:
    return ABJournalWriter(
        directory=directory,
        experiment_id="bounded_research_planner_ab",
        run_id=run_id,
        provider=provider,
        model="deepseek-chat",
    )


def _simulate_case(writer: ABJournalWriter, *, case: str, arm: str) -> None:
    writer.record_case_start(case=case, arm=arm, question_chars=20)
    writer.record_send_start(case=case, arm=arm, turn=1, prompt="question one")
    writer.record_reply(case=case, arm=arm, turn=1, prompt="question one", reply="answer one")
    writer.record_case_complete(
        case=case,
        arm=arm,
        row={"ok": True, "score": 6, "stop_reason": "done", "turns": 3},
    )


def test_manifest_is_atomic_and_identity_bound(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    try:
        writer.record_run_start(cases=("widget_noop",), arms=("baseline",), max_turns=5)
    finally:
        writer.close()

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["experiment_id"] == "bounded_research_planner_ab"
    assert manifest["run_id"] == "run-1"
    assert manifest["provider"] == "deepseek"
    assert manifest["status"] == "running"

    # Reopening the same identity is fine; a different identity fails closed.
    again = _writer(directory)
    again.close()
    with pytest.raises(ABJournalIdentityMismatch):
        _writer(directory, run_id="run-2")


def test_events_form_a_verifiable_hash_chain(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    try:
        writer.record_run_start(cases=("a",), arms=("baseline", "planner"), max_turns=5)
        _simulate_case(writer, case="a", arm="baseline")
        writer.record_run_complete(rows=1)
    finally:
        writer.close()

    reader = ABJournalReader(directory)
    events = reader.events()
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert reader.verify_hash_chain() == []
    # Chain linkage is real: each event references its predecessor.
    assert events[0]["previous_digest"].startswith("sha256:")
    for previous, event in zip(events, events[1:]):
        assert event["previous_digest"] == previous["event_digest"]


def test_corrupt_tail_line_is_recoverable(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    try:
        _simulate_case(writer, case="a", arm="baseline")
    finally:
        writer.close()
    events_path = directory / "events.jsonl"

    with events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 5, "truncated": ')

    reader = ABJournalReader(directory)
    assert reader.recover_tail() >= 1
    assert reader.verify_hash_chain() == []
    assert ("a", "baseline") in reader.completed_case_keys()

    # A new writer can continue the recovered chain seamlessly.
    resumed = _writer(directory)
    try:
        _simulate_case(resumed, case="a", arm="planner")
    finally:
        resumed.close()
    assert ABJournalReader(directory).verify_hash_chain() == []


def test_mid_file_tampering_stays_visible(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    try:
        writer.record_run_start(cases=("a",), arms=("baseline",), max_turns=5)
        _simulate_case(writer, case="a", arm="baseline")
    finally:
        writer.close()
    events_path = directory / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["facts"]["ok"] = False  # forge history without fixing digests
    lines[1] = json.dumps(tampered, ensure_ascii=False, sort_keys=True)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    problems = ABJournalReader(directory).verify_hash_chain()
    assert any(problem.startswith("bad-digest-at-seq:2") for problem in problems)

    # The writer refuses to append onto a journal it cannot verify.
    with pytest.raises(ValueError, match="journal verification"):
        _writer(directory)


def test_duplicate_seq_is_detected(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    try:
        _simulate_case(writer, case="a", arm="baseline")
    finally:
        writer.close()
    events_path = directory / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    lines.append(lines[-1])  # duplicate the final line verbatim

    problems = __import__(
        "tests.manual.ab_journal", fromlist=["verify_event_chain"]
    ).verify_event_chain([json.loads(line) for line in lines])
    # The verbatim duplicate keeps its own digest, so the chain itself is
    # intact; what breaks is sequence uniqueness and contiguity.
    assert any(problem.startswith("duplicate-seq:") for problem in problems)
    assert any(problem.startswith("seq-gap:") for problem in problems)


def test_completed_case_keys_drive_resume(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    try:
        _simulate_case(writer, case="widget_noop", arm="baseline")
        _simulate_case(writer, case="widget_noop", arm="planner")
    finally:
        writer.close()

    reader = ABJournalReader(directory)
    assert reader.completed_case_keys() == [
        ("widget_noop", "baseline"),
        ("widget_noop", "planner"),
    ]


def test_digest_only_mode_writes_no_transcript_files(tmp_path: Path) -> None:
    directory = journal_directory_for(tmp_path / "result.json")
    writer = _writer(directory)
    marker_prompt = "PROMPT-RAW-MUST-NOT-PERSIST"
    marker_reply = "REPLY-RAW-MUST-NOT-PERSIST"
    try:
        writer.record_reply(
            case="a", arm="baseline", turn=1, prompt=marker_prompt, reply=marker_reply
        )
    finally:
        writer.close()

    assert not (directory / "transcripts").exists()
    serialized = json.dumps(ABJournalReader(directory).events(), ensure_ascii=False)
    assert marker_prompt not in serialized
    assert marker_reply not in serialized
    reply_event = ABJournalReader(directory).events()[-1]
    assert reply_event["content_ref"]["mode"] == "digest_only"
    assert reply_event["content_ref"]["path"] == ""
    assert reply_event["prompt_digest"].startswith("sha256:")
    assert reply_event["reply_digest"].startswith("sha256:")
