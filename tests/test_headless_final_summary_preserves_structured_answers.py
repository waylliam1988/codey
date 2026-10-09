"""Final JSONL answers are deliveries, rather than short progress previews."""

import json

import pytest

from codey.app.event_payloads import machine_event_payload
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.runtime.core.run_result import RunResult


def test_final_summary_preserves_long_structured_answer():
    summary = json.dumps({"reason": "诊断证据 " * 400, "status": "blocked"}, ensure_ascii=False)
    payload = machine_event_payload({"type": "task_done", "summary": summary})
    assert payload is not None
    assert payload["summary"] == summary
    assert json.loads(str(payload["summary"]))["status"] == "blocked"
    assert not payload.get("summary_truncated", False)


def test_oversized_final_summary_reports_truncation_explicitly():
    payload = machine_event_payload({"type": "task_done", "summary": "x" * 65_000})
    assert payload is not None
    assert len(str(payload["summary"])) <= 64_000
    assert payload["summary_truncated"] is True


def test_progress_previews_keep_the_existing_small_bound():
    payload = machine_event_payload({"type": "info", "text": "x" * 2_000})
    assert payload is not None
    assert len(str(payload["text"])) <= 1_000


@pytest.mark.usefixtures("no_external_advisor_models")
def test_real_headless_delivery_keeps_the_complete_final_answer(tmp_path):
    summary = json.dumps({"reason": "read-only finding " * 150, "status": "blocked"})
    rows = []

    class Provider:
        name = "Fixture"

        def close(self):
            pass

    result = run_headless(
        HeadlessRequest(project=tmp_path / "project", task="diagnose", provider_id="qwen",
                        state_home=tmp_path / "state", project_changes_required=False),
        emit_jsonl=rows.append,
        agent_run=lambda *_args, **_kwargs: RunResult(summary, "done", 1),
        collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
        connect_provider=lambda *_args, **_kwargs: Provider(),
    )
    assert result.exit_code == 0
    terminal = next(row for row in rows if row["type"] == "task_done")
    assert terminal["summary"] == summary
    assert json.loads(terminal["summary"])["status"] == "blocked"
