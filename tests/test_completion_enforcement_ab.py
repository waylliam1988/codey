from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.manual import completion_enforcement_ab as harness
from tests.manual import ab_harness_common as common
from tests.manual.ab_harness_common import open_journal_for_output


def test_live_resume_skips_completed_rows_without_opening_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "completion_enforcement_ab-deepseek.json"
    row = {
        "case": "premature_done_no_test",
        "arm": "control_done",
        "stop_reason": "done",
        "false_completion": True,
    }
    output.write_text(
        json.dumps(
            {
                "probe": "completion_enforcement_ab",
                "provider": "deepseek",
                "cases": ["premature_done_no_test"],
                "arms": ["control_done"],
                "complete": False,
                "rows": [row],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    def connect_provider(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("provider should not open when every row is present")

    monkeypatch.setattr("codey.providers.registry.connect_provider", connect_provider)

    report = harness.run_live(
        "deepseek",
        9222,
        ("premature_done_no_test",),
        ("control_done",),
        3,
        output=output,
        transcript_mode="digest-only",
    )

    assert report["complete"] is True
    assert report["rows"] == [row]
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["rows"] == [row]


def test_completion_enforcement_journal_identity_is_stable_for_fixed_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "fixed-output.json"
    first = open_journal_for_output(
        output=output,
        experiment_id="completion_enforcement_ab",
        provider_id="deepseek",
        transcript_mode="digest-only",
        max_turns=4,
        case_names=("premature_done_no_test",),
        arms=("control_done",),
    )
    assert first is not None
    try:
        assert first.run_id == "deepseek-fixed-output"
    finally:
        first.close()

    second = open_journal_for_output(
        output=output,
        experiment_id="completion_enforcement_ab",
        provider_id="deepseek",
        transcript_mode="digest-only",
        max_turns=4,
        case_names=("premature_done_no_test",),
        arms=("control_done",),
    )
    assert second is not None
    try:
        assert second.event_count == 2
    finally:
        second.close()


def test_live_rerun_failed_keeps_old_error_when_provider_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "completion_enforcement_ab-deepseek.json"
    old_error = {
        "case": "premature_done_no_test",
        "arm": "control_done",
        "error": "TimeoutError: send",
        "stop_reason": "error",
    }
    output.write_text(
        json.dumps(
            {
                "probe": "completion_enforcement_ab",
                "provider": "deepseek",
                "cases": ["premature_done_no_test"],
                "arms": ["control_done"],
                "complete": False,
                "rows": [old_error],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    def connect_provider(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr("codey.providers.registry.connect_provider", connect_provider)

    with pytest.raises(RuntimeError):
        harness.run_live(
            "deepseek",
            9222,
            ("premature_done_no_test",),
            ("control_done",),
            3,
            output=output,
            transcript_mode="digest-only",
            rerun_failed=True,
        )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["rows"] == [old_error]
    assert not (tmp_path / "completion_enforcement_ab-deepseek-journal").exists()


def test_rerun_failed_replaces_old_error_row_only_after_new_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "completion_enforcement_ab-deepseek.json"
    old_error = {
        "case": "premature_done_no_test",
        "arm": "control_done",
        "error": "TimeoutError: send",
        "stop_reason": "error",
    }
    output.write_text(
        json.dumps(
            {
                "probe": "completion_enforcement_ab",
                "provider": "deepseek",
                "cases": ["premature_done_no_test"],
                "arms": ["control_done"],
                "complete": False,
                "rows": [old_error],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    class FakeProvider:
        def close(self) -> None:
            pass

    def live_project(root: Path, _spec: dict[str, object]) -> Path:
        project = root / "project"
        project.mkdir()
        raise RuntimeError("fixture failed before row")

    monkeypatch.setattr("codey.providers.registry.connect_provider", lambda *_a, **_k: FakeProvider())
    monkeypatch.setattr(harness, "_live_project", live_project)

    report = harness.run_live(
        "deepseek",
        9222,
        ("premature_done_no_test",),
        ("control_done",),
        3,
        output=output,
        transcript_mode="digest-only",
        rerun_failed=True,
    )

    matching = [
        row for row in report["rows"] if row["case"] == "premature_done_no_test" and row["arm"] == "control_done"
    ]
    assert len(matching) == 1
    assert matching[0]["error"] == "RuntimeError: fixture failed before row"
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["rows"] == report["rows"]


def test_rerun_failed_replaces_old_error_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "completion_enforcement_ab-deepseek.json"
    old_error = {
        "case": "premature_done_no_test",
        "arm": "control_done",
        "error": "TimeoutError: send",
        "stop_reason": "error",
    }
    untouched_error = {
        "case": "fresh_failing_test_after_edit",
        "arm": "control_done",
        "error": "TimeoutError: send",
        "stop_reason": "error",
    }
    output.write_text(
        json.dumps(
            {
                "probe": "completion_enforcement_ab",
                "provider": "deepseek",
                "cases": ["premature_done_no_test", "fresh_failing_test_after_edit"],
                "arms": ["control_done"],
                "complete": False,
                "rows": [old_error, untouched_error],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )

    class FakeProvider:
        def close(self) -> None:
            pass

    class FakeRunner:
        def run(self, _request: object) -> None:
            pass

    def live_project(root: Path, _spec: dict[str, object]) -> Path:
        project = root / "project"
        project.mkdir()
        return project

    def finish_row(**kwargs: object) -> dict[str, object]:
        return {
            "case": kwargs["case_name"],
            "arm": kwargs["arm"],
            "stop_reason": "done",
            "false_completion": False,
            "blocked_honestly": False,
            "unnecessary_repair": False,
            "repair_rounds": 0,
            "repair_success": False,
            "regression_after_repair": False,
            "independent_ok": True,
            "repair_context_chars": 0,
            "writer_phases": 1,
            "tool_calls": 0,
            "turns": 1,
            "elapsed_s": 0.01,
        }

    monkeypatch.setattr("codey.providers.registry.connect_provider", lambda *_a, **_k: FakeProvider())
    monkeypatch.setattr(harness, "_live_project", live_project)
    monkeypatch.setattr(harness, "_build_runner", lambda *_a, **_k: FakeRunner())
    monkeypatch.setattr(harness, "_finish_row", finish_row)

    report = harness.run_live(
        "deepseek",
        9222,
        ("premature_done_no_test",),
        ("control_done",),
        3,
        output=output,
        transcript_mode="digest-only",
        rerun_failed=True,
    )

    matching = [
        row for row in report["rows"] if row["case"] == "premature_done_no_test" and row["arm"] == "control_done"
    ]
    assert len(matching) == 1
    assert "error" not in matching[0]
    assert untouched_error in report["rows"]


def test_live_terminal_error_row_fails_report_and_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "completion_enforcement_ab-deepseek.json"

    class FakeProvider:
        def close(self) -> None:
            pass

    class FakeRunner:
        def run(self, _request: object) -> None:
            pass

    def live_project(root: Path, _spec: dict[str, object]) -> Path:
        project = root / "project"
        project.mkdir()
        return project

    def finish_row(**kwargs: object) -> dict[str, object]:
        return {
            "case": kwargs["case_name"],
            "arm": kwargs["arm"],
            "stop_reason": "error",
            "false_completion": False,
            "blocked_honestly": False,
            "unnecessary_repair": False,
            "repair_rounds": 0,
            "repair_success": False,
            "regression_after_repair": False,
            "independent_ok": False,
            "repair_context_chars": 0,
            "writer_phases": 1,
            "tool_calls": 0,
            "turns": 0,
            "elapsed_s": 0.01,
        }

    monkeypatch.setattr("codey.providers.registry.connect_provider", lambda *_a, **_k: FakeProvider())
    monkeypatch.setattr(harness, "_live_project", live_project)
    monkeypatch.setattr(harness, "_build_runner", lambda *_a, **_k: FakeRunner())
    monkeypatch.setattr(harness, "_finish_row", finish_row)

    report = harness.run_live(
        "deepseek",
        9222,
        ("premature_done_no_test",),
        ("control_done",),
        3,
        output=output,
        transcript_mode="digest-only",
    )

    assert report["ok"] is False
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["ok"] is False
    assert saved["rows"][0]["stop_reason"] == "error"
    assert saved["rows"][0]["codey_failure_class"] == common.AB_FAILURE_CODEY
    assert saved["rows"][0]["provider_error_class"] == common.AB_FAILURE_NONE

    events_path = harness.journal_directory_for_output(output) / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    case_complete = [event for event in events if event["event_type"] == "case_complete"][-1]
    assert case_complete["facts"]["ok"] is False
    run_complete = [event for event in events if event["event_type"] == "run_complete"][-1]
    assert run_complete["facts"]["status"] == "failed"


def test_finish_row_preserves_terminal_error_summary(tmp_path: Path) -> None:
    class FakeState:
        last_terminal_event = {
            "stop_reason": "error",
            "summary": "ERROR: provider setup failed",
        }
        run_traces = None

    row = harness._finish_row(
        case_name="premature_done_no_test",
        arm="control_done",
        state=FakeState(),  # type: ignore[arg-type]
        session_suffix="unit",
        project=tmp_path,
        observed={},
        writer_phases=None,
        tool_calls=0,
        turns=0,
        elapsed_s=0.01,
        independent_ok=False,
    )

    assert row["error"] == "ERROR: provider setup failed"


def test_terminal_provider_no_reply_is_classified() -> None:
    class FakeTracing:
        send_index = 1
        reply_count = 0

    row = {
        "case": "dependency_missing_env_failure",
        "arm": "control_done",
        "stop_reason": "error",
        "error": "ERROR: DeepSeek Web new_chat failed (transient)",
    }

    harness._attach_terminal_failure_classes(row, FakeTracing())  # type: ignore[arg-type]

    assert row["provider_failure_class"] == common.PROVIDER_FAILURE_NO_REPLY
    assert row["provider_error_class"] == common.PROVIDER_FAILURE_NO_REPLY
    assert row["codey_failure_class"] == common.AB_FAILURE_NONE


def test_build_runner_live_path_uses_real_callables(tmp_path: Path) -> None:
    state = harness.server.State(tmp_path / "state")

    runner = harness._build_runner(state)

    assert runner.agent_run is harness.default_agent_run
    assert runner.collect_changes is harness.default_collect_changes
