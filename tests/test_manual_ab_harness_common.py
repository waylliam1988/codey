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
    events = [
        json.loads(line)
        for line in (journal_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
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

    resumed = common.load_or_new_payload(
        output, probe="p", provider_id="qwen", cases=("c1",), arms=("a",)
    )
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
