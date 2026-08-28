from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.manual import ab_harness_common as common


class _EchoProvider:
    id = "echo"
    name = "Echo"
    location = "web"

    def __init__(self) -> None:
        self.replies = ["r1", "r2"]
        self.closed = False

    def new_chat(self, timeout=None):
        return object()

    def send(self, text, timeout=None):
        if not self.replies:
            raise AssertionError("echo provider ran out of replies")
        return self.replies.pop(0)

    def close(self):
        self.closed = True
        return None


def test_arm_schedule_interleaves_and_is_stable() -> None:
    one_repeat = common.interleaved_arm_schedule(("baseline", "projection"), 1)
    two_repeats = common.interleaved_arm_schedule(("baseline", "projection"), 2)

    assert [arm for arm, _ in one_repeat] == ["baseline", "projection"]
    assert [arm for arm, _ in two_repeats] == [
        "baseline",
        "projection",
        "projection",
        "baseline",
    ]
    assert [repeat for _, repeat in two_repeats] == [1, 1, 2, 2]
    assert common.interleaved_arm_schedule(("baseline", "projection"), 0) == one_repeat


def _row(case: str, arm: str, repeat: int) -> dict:
    return {"case": case, "arm": arm, "repeat": repeat}


def test_matrix_complete_accepts_only_the_full_matrix() -> None:
    complete = [
        _row("c1", "a", 1),
        _row("c1", "b", 1),
    ]
    assert common.matrix_complete(complete, arms=("a", "b"), cases=("c1",), repeats=1)

    missing_arm = [_row("c1", "a", 1)]
    assert not common.matrix_complete(missing_arm, arms=("a", "b"), cases=("c1",), repeats=1)

    duplicate = [*complete, _row("c1", "b", 1)]
    assert not common.matrix_complete(duplicate, arms=("a", "b"), cases=("c1",), repeats=1)

    missing_repeat = [
        _row("c1", "a", 1),
        _row("c1", "b", 1),
    ]
    assert not common.matrix_complete(missing_repeat, arms=("a", "b"), cases=("c1",), repeats=2)


def test_tracing_provider_true_pass_through_for_scripted_providers() -> None:
    class _BareProvider:
        name = "bare"

        def __init__(self) -> None:
            self.chat_calls: list[bool] = []
            self.sent: list[str] = []

        def new_chat(self):
            self.chat_calls.append(True)
            return object()

        def send(self, text):
            self.sent.append(text)
            return "ok"

    bare = _BareProvider()
    provider = common.TracingProvider(bare)

    assert provider.new_chat() is not None
    assert bare.chat_calls == [True]
    assert provider.send("hi") == "ok"
    assert bare.sent == ["hi"]
    provider.close()  # No close() on the wrapped provider: silent no-op.


def test_tracing_provider_forwards_timeouts_only_when_configured() -> None:
    class _TimeoutSpy:
        name = "spy"

        def __init__(self) -> None:
            self.send_calls: list[dict] = []
            self.chat_calls: list[dict] = []
            self.closed = False

        def new_chat(self, *args, **kwargs):
            self.chat_calls.append({"args": args, "kwargs": kwargs})
            return object()

        def send(self, *args, **kwargs):
            self.send_calls.append({"args": args, "kwargs": kwargs})
            return "r"

        def close(self):
            self.closed = True
            return None

    passthrough = _TimeoutSpy()
    plain = common.TracingProvider(passthrough)
    plain.send("a")
    assert passthrough.send_calls == [{"args": ("a",), "kwargs": {}}]
    plain.new_chat()
    assert passthrough.chat_calls == [{"args": (), "kwargs": {}}]

    configured = _TimeoutSpy()
    wrapped = common.TracingProvider(configured, timeout=7.5, new_chat_timeout=3.0)
    wrapped.send("b")
    wrapped.new_chat()
    assert configured.send_calls == [{"args": ("b",), "kwargs": {"timeout": 7.5}}]
    assert configured.chat_calls == [{"args": (), "kwargs": {"timeout": 3.0}}]


def test_tracing_provider_journals_send_reply(tmp_path: Path) -> None:
    from tests.manual.ab_journal import (
        ABJournalWriter,
        TranscriptReplayCache,
        TRANSCRIPT_MODE_ARCHIVE,
        verify_event_chain,
    )

    journal_dir = tmp_path / "journal"
    journal = ABJournalWriter(
        directory=journal_dir,
        experiment_id="ab_harness_common",
        run_id="unit-test",
        provider="echo",
        transcript_cache=TranscriptReplayCache(journal_dir, mode=TRANSCRIPT_MODE_ARCHIVE),
    )
    provider = common.TracingProvider(
        _EchoProvider(),
        journal=journal,
        case="case-a",
        arm="arm-a",
        timeout=10.0,
        new_chat_timeout=5.0,
    )

    reply = provider.send("hello prompt")

    assert reply == "r1"
    assert provider.prompts == ["hello prompt"]
    assert provider.replies == ["r1"]
    assert provider.send_index == 1
    assert provider.last_reply == "r1"
    journal.close()
    events = [json.loads(line) for line in (journal_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [event.get("event_type") or event.get("type") for event in events]
    assert any(kind == "send_start" for kind in kinds), kinds
    assert any(kind == "reply" for kind in kinds), kinds
    assert verify_event_chain(events) == []


def test_tracing_provider_without_journal_still_counts() -> None:
    provider = common.TracingProvider(_EchoProvider())

    provider.send("a")
    provider.send("b")

    assert provider.prompts == ["a", "b"]
    assert provider.replies == ["r1", "r2"]
    assert provider.reply_count == 2
    assert provider.last_turn == 2


def test_tracing_provider_records_send_errors(tmp_path: Path) -> None:
    from tests.manual.ab_journal import ABJournalReader, ABJournalWriter

    journal_dir = tmp_path / "journal"
    journal = ABJournalWriter(
        directory=journal_dir,
        experiment_id="ab_harness_common",
        run_id="error-test",
        provider="echo",
    )

    class _FailingProvider(_EchoProvider):
        def send(self, text, timeout=None):
            raise RuntimeError("boom")

    provider = common.TracingProvider(_FailingProvider(), journal=journal, case="c", arm="a")
    with pytest.raises(RuntimeError):
        provider.send("prompt")
    journal.close()

    events = ABJournalReader(journal_dir).events()
    assert events[-1]["event_type"] == "send_error"


def test_atomic_writer_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "result.json"
    common.write_json_atomic(path, {"ok": True, "rows": [1, 2]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True, "rows": [1, 2]}
    assert not list(tmp_path.glob(".result.json.tmp*"))


def test_write_payload_bounded_enforces_size_cap(tmp_path: Path) -> None:
    path = tmp_path / "big.json"
    with pytest.raises(ValueError):
        common.write_payload_bounded(
            path,
            {"blob": "x" * (common.MAX_RESULT_BYTES + 1)},
        )
    assert not path.exists()


def test_provider_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    output.write_text(
        json.dumps({"probe": "p", "provider": "qwen", "rows": []}),
        encoding="utf-8",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    with pytest.raises(common.OutputProviderMismatch) as excinfo:
        common.ensure_output_provider_identity(payload, provider_id="deepseek", output=output)
    assert excinfo.value.expected == "deepseek"
    assert excinfo.value.found == "qwen"

    resumed = common.load_or_new_payload(output, probe="p", provider_id="qwen", cases=("c1",), arms=("a",))
    assert resumed["complete"] is False
    assert isinstance(resumed["rows"], list)


def test_load_or_new_payload_starts_fresh_when_absent(tmp_path: Path) -> None:
    payload = common.load_or_new_payload(
        tmp_path / "missing.json",
        probe="probe_x",
        provider_id="deepseek",
        cases=(["c1", "c2"],),
        arms=("a", "b"),
    )
    assert payload["probe"] == "probe_x"
    assert payload["provider"] == "deepseek"
    assert payload["cases"] == ["c1", "c2"]
    assert payload["arms"] == ["a", "b"]
    assert payload["complete"] is False
    assert payload["rows"] == []


def test_normalize_payload_metadata_merges_unique_names() -> None:
    payload = {"cases": ["c1"], "arms": ["a"]}
    common.normalize_payload_metadata(
        payload,
        provider_id="deepseek",
        cases=(["c1", "c2"], ["c2"]),
        arms=("a", "b"),
    )
    assert payload["cases"] == ["c1", "c2"]
    assert payload["arms"] == ["a", "b"]
    assert common.merge_unique_names(None, "x", ("x", "y")) == ["x", "y"]


def test_case_names_accepts_objects_or_strings() -> None:
    class _Case:
        name = "named"

    assert common.case_names((_Case(), "plain")) == ["named", "plain"]


def test_arm_run_layout_binds_result_journal_manifest_and_transcripts(tmp_path: Path) -> None:
    output = tmp_path / "suite" / "result.json"
    layout = common.ArmRunLayout.for_output(output)

    assert layout.output_json == output
    assert layout.journal_dir == tmp_path / "suite" / "result.trace"
    assert layout.manifest_path == tmp_path / "suite" / "result-manifest.json"
    assert layout.transcript_dir == layout.journal_dir / "transcripts"


def test_result_row_store_replaces_failed_row_for_fixed_output(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    store = common.ResultRowStore.open(
        output,
        probe="probe",
        provider_id="deepseek",
        cases=("case-a",),
        arms=("arm-a",),
        summarize=common.summarize_arm_rows,
    )
    store.upsert({"case": "case-a", "arm": "arm-a", "repeat": 1, "error": "boom"})

    resumed = common.ResultRowStore.open(
        output,
        probe="probe",
        provider_id="deepseek",
        cases=("case-a",),
        arms=("arm-a",),
        summarize=common.summarize_arm_rows,
    )
    assert resumed.pending_keys(cases=("case-a",), arms=("arm-a",), rerun_failed=True) == [("case-a", "arm-a", 1)]
    resumed.upsert({"case": "case-a", "arm": "arm-a", "repeat": 1, "ok": True}, complete=True)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert payload["ok"] is True
    assert payload["summary"]["rows"] == 1
    assert payload["summary"]["errors"] == 0
    assert payload["rows"] == [{"case": "case-a", "arm": "arm-a", "repeat": 1, "ok": True}]


def test_upsert_case_row_uses_sample_when_repeat_is_absent() -> None:
    rows = [
        {"provider": "deepseek", "case": "case-a", "arm": "batch", "sample": 1, "ok": True},
        {"provider": "deepseek", "case": "case-a", "arm": "batch", "sample": 2, "error": "old"},
    ]

    common.upsert_case_row(
        rows,
        {"provider": "deepseek", "case": "case-a", "arm": "batch", "sample": 2, "ok": True},
        provider_id="deepseek",
    )

    assert len(rows) == 2
    assert rows[0]["sample"] == 1
    assert rows[1]["sample"] == 2
    assert rows[1]["ok"] is True
    assert "error" not in rows[1]


def test_result_row_store_treats_terminal_error_as_failed(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    store = common.ResultRowStore.open(
        output,
        probe="probe",
        provider_id="deepseek",
        cases=("case-a",),
        arms=("arm-a",),
        summarize=common.summarize_arm_rows,
    )
    store.upsert({"case": "case-a", "arm": "arm-a", "repeat": 1, "stop_reason": "error"})

    failed = json.loads(output.read_text(encoding="utf-8"))
    assert failed["ok"] is False
    assert common.row_has_terminal_failure(failed["rows"][0])

    resumed = common.ResultRowStore.open(
        output,
        probe="probe",
        provider_id="deepseek",
        cases=("case-a",),
        arms=("arm-a",),
        summarize=common.summarize_arm_rows,
    )
    assert resumed.pending_keys(cases=("case-a",), arms=("arm-a",), rerun_failed=True) == [("case-a", "arm-a", 1)]


def test_result_row_store_pending_does_not_destroy_old_evidence(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    output.write_text(
        json.dumps(
            {
                "probe": "probe",
                "provider": "deepseek",
                "rows": [{"case": "case-a", "arm": "arm-a", "repeat": 1, "error": "old"}],
            }
        ),
        encoding="utf-8",
    )
    before = output.read_text(encoding="utf-8")

    store = common.ResultRowStore.open(
        output,
        probe="probe",
        provider_id="deepseek",
        cases=("case-a",),
        arms=("arm-a",),
    )

    assert store.pending_keys(cases=("case-a",), arms=("arm-a",), rerun_failed=True)
    assert output.read_text(encoding="utf-8") == before


def test_result_row_store_allows_domain_ok_gate(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    store = common.ResultRowStore.open(
        output,
        probe="probe",
        provider_id="deepseek",
        cases=("case-a",),
        arms=("arm-a",),
        ok=lambda rows, complete: bool(complete and rows and rows[0].get("exact")),
    )

    store.upsert({"case": "case-a", "arm": "arm-a", "repeat": 1, "exact": True}, complete=False)
    assert json.loads(output.read_text(encoding="utf-8"))["ok"] is False

    store.write(complete=True)
    assert json.loads(output.read_text(encoding="utf-8"))["ok"] is True


def test_bind_row_evidence_refs_and_transcript_path(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    layout = common.ArmRunLayout.for_output(output)
    transcript = layout.journal_dir / "transcripts" / ("a" * 64 + ".json")
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}", encoding="utf-8")

    provider = _EchoProvider()
    provider.transcript_refs = [  # type: ignore[attr-defined]
        {
            "mode": "archive",
            "content_digest": "sha256:" + "a" * 64,
            "path": f"transcripts/{'a' * 64}.json",
        }
    ]

    row = common.bind_row_evidence_refs(
        {"case": "case-a", "arm": "arm-a"},
        layout=layout,
        tracing_provider=provider,  # type: ignore[arg-type]
    )

    assert row["output_json"] == str(output)
    assert row["journal_dir"] == str(layout.journal_dir)
    assert row["transcript_replayable"] is True
    assert common.transcript_path_for_row(row, layout=layout) == transcript


def test_digest_only_transcript_ref_is_not_replayable(tmp_path: Path) -> None:
    layout = common.ArmRunLayout.for_output(tmp_path / "result.json")
    provider = _EchoProvider()
    provider.transcript_refs = [  # type: ignore[attr-defined]
        {
            "mode": "digest_only",
            "content_digest": "sha256:" + "b" * 64,
            "path": "",
        }
    ]

    row = common.bind_row_evidence_refs(
        {"case": "case-a", "arm": "arm-a"},
        layout=layout,
        tracing_provider=provider,  # type: ignore[arg-type]
    )

    assert row["transcript_replayable"] is False
    assert common.transcript_path_for_row(row, layout=layout) is None


def test_provider_failure_classifier_uses_closed_vocabulary() -> None:
    assert (
        common.classify_provider_failure(sends=1, replies=0, error=TimeoutError("timed out"))
        == common.PROVIDER_FAILURE_NATIVE_SEARCH_STALL
    )
    assert (
        common.classify_provider_failure(error=RuntimeError("browser target closed"))
        == common.PROVIDER_FAILURE_SEND_ERROR
    )
    assert (
        common.classify_provider_failure(error=RuntimeError("selector not visible"))
        == common.PROVIDER_FAILURE_WEBPAGE_UI_CHANGED
    )
    assert common.classify_provider_failure() == common.PROVIDER_FAILURE_NONE


def test_arm_manifest_accepts_provider_failure_class(tmp_path: Path) -> None:
    manifest = common.build_arm_manifest(
        suite="suite",
        provider="deepseek",
        arms=("arm-a",),
        cases=("case-a",),
        max_turns=4,
        journal_dir=tmp_path / "trace",
        transcript_mode="digest-only",
        started_at="2026-08-28T00:00:00Z",
        provider_error_class=common.PROVIDER_FAILURE_NATIVE_SEARCH_STALL,
        repo=tmp_path,
    )

    assert manifest["provider_error_class"] == common.PROVIDER_FAILURE_NATIVE_SEARCH_STALL


def test_open_journal_for_output_records_resume_attempt(tmp_path: Path) -> None:
    from tests.manual.ab_journal import ABJournalReader

    output = tmp_path / "result.json"
    first = common.open_journal_for_output(
        output=output,
        experiment_id="resume-test",
        provider_id="deepseek",
        transcript_mode="digest-only",
        case_names=("case-a",),
        arms=("arm-a",),
        max_turns=4,
    )
    assert first is not None
    first.record_run_complete(rows=0)
    first.close()

    second = common.open_journal_for_output(
        output=output,
        experiment_id="resume-test",
        provider_id="deepseek",
        transcript_mode="digest-only",
        case_names=("case-a",),
        arms=("arm-a",),
        max_turns=4,
    )
    assert second is not None
    second.close()

    events = ABJournalReader(common.journal_directory_for_output(output)).events()
    starts = [event for event in events if event["event_type"] == "run_start"]
    assert len(starts) == 2
    assert starts[0]["facts"]["resumed_attempt"] is False
    assert starts[1]["facts"]["resumed_attempt"] is True
    assert starts[0]["facts"]["attempt_index"] == 1
    assert starts[1]["facts"]["attempt_index"] == 2


def test_fixture_search_provider_matches_legacy_behavior() -> None:
    docs = (
        common.FixtureDocument(
            url="https://source-a.test/x",
            title="A",
            text="stable-v2 endpoint guidance",
            keywords=("primary",),
            default=True,
        ),
        common.FixtureDocument(
            url="https://source-b.test/y",
            title="B",
            text="fresh update material",
            keywords=("update", "current"),
        ),
    )
    fixture = common.FixtureSearchProvider(docs)

    default_only = fixture.search("anything unrelated")
    assert [item["url"] for item in default_only] == ["https://source-a.test/x"]

    with common.fixture_material_phase(fixture):
        matches = fixture.search("current update")
    assert [item["url"] for item in matches] == [
        "https://source-b.test/y",
        "https://source-a.test/x",
    ]

    fetched = fixture.fetch("https://source-a.test/x")
    assert fetched["title"] == "A"
    assert fixture.fetch("https://missing.test/z")["text"].startswith("ERROR:")
