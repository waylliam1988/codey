from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.manual import verification_review_ab as harness
from tests.manual.ab_journal import ABJournalReader


class FakeProvider:
    name = "fake-reviewer"
    location = "fake://reviewer"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0
        self.closed = False

    def new_chat(self) -> None:
        self.new_chat_calls += 1

    def send(self, text: str) -> str:
        self.sent.append(text)
        if not self.replies:
            raise RuntimeError("no scripted reply")
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True


def _review_reply() -> str:
    return json.dumps(
        {
            "verdict": "changes_requested",
            "summary": "Verification is still missing.",
            "findings": [
                {
                    "path": "src/auth.py",
                    "issue": "No relevant check was observed after the edit.",
                    "suggested_fix": "Run tests/test_auth.py or python -m pytest.",
                }
            ],
        }
    )


def test_self_test_entrypoint() -> None:
    assert harness.main(["--self-test"]) == 0


def test_prompt_arm_only_adds_concrete_verification_map_candidate() -> None:
    baseline = harness._prompt_for(harness.AUTH_REVIEW_CASE, "baseline")
    current = harness._prompt_for(harness.AUTH_REVIEW_CASE, "current")

    assert "tests/test_auth.py" not in baseline
    assert "tests/test_auth.py" in current


def test_live_run_writes_fixed_evidence_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = FakeProvider(_review_reply())
    monkeypatch.setattr(
        "tests.manual.verification_review_ab.connect_provider",
        lambda *_args, **_kwargs: provider,
    )
    output = tmp_path / "verification_review_ab-deepseek-current.json"

    payload = harness.run_live(
        "deepseek",
        "current",
        9222,
        output=output,
        transcript_mode="archive",
    )

    assert payload["ok"] is True
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["protocol_ok"] is True
    assert row["transcript_replayable"] is True
    assert row["output_json"] == str(output)
    assert provider.closed is True
    assert provider.new_chat_calls == 1

    manifest = json.loads(output.with_name("verification_review_ab-deepseek-current-manifest.json").read_text())
    assert manifest["suite"] == "verification_review_ab"
    assert manifest["provider"] == "deepseek"
    assert manifest["transcript_mode"] == "archive"
    events = ABJournalReader(output.with_name("verification_review_ab-deepseek-current.trace")).events()
    assert [event["event_type"] for event in events] == [
        "run_start",
        "case_start",
        "send_start",
        "reply",
        "case_complete",
        "run_complete",
    ]


def test_live_resume_skips_completed_row_without_opening_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "verification_review_ab-deepseek-current.json"
    row = {
        "case": harness.AUTH_REVIEW_CASE.name,
        "arm": "current",
        "protocol_ok": True,
        "stop_reason": "done",
    }
    output.write_text(
        json.dumps(
            {
                "probe": "verification_review_ab",
                "provider": "deepseek",
                "cases": [harness.AUTH_REVIEW_CASE.name],
                "arms": ["current"],
                "complete": False,
                "rows": [row],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    def connect_provider(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("provider should not open when row is complete")

    monkeypatch.setattr("tests.manual.verification_review_ab.connect_provider", connect_provider)

    payload = harness.run_live(
        "deepseek",
        "current",
        9222,
        output=output,
        transcript_mode="archive",
    )

    assert payload["rows"] == [row]


def test_rerun_failed_keeps_old_row_when_provider_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "verification_review_ab-deepseek-current.json"
    old_error = {
        "case": harness.AUTH_REVIEW_CASE.name,
        "arm": "current",
        "error": "TimeoutError: send",
        "stop_reason": "error",
    }
    output.write_text(
        json.dumps(
            {
                "probe": "verification_review_ab",
                "provider": "deepseek",
                "cases": [harness.AUTH_REVIEW_CASE.name],
                "arms": ["current"],
                "complete": False,
                "rows": [old_error],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    def connect_provider(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr("tests.manual.verification_review_ab.connect_provider", connect_provider)

    with pytest.raises(RuntimeError):
        harness.run_live(
            "deepseek",
            "current",
            9222,
            output=output,
            transcript_mode="archive",
            rerun_failed=True,
        )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["rows"] == [old_error]
