"""Final JSONL answers are deliveries, rather than short progress previews."""

import json
from pathlib import Path

import pytest

from codey.agents.request import AgentRequest
from codey.app.event_payloads import machine_event_payload
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.base import ChatProvider
from codey.runtime.core.run_result import RunResult
from codey.utils.object_fields import decoded_object
from codey.workspace.changes import ChangeTracker


def test_final_summary_preserves_long_structured_answer() -> None:
    answer: dict[str, object] = {"reason": "诊断证据 " * 400, "status": "blocked"}
    summary = json.dumps(answer, ensure_ascii=False)
    payload = machine_event_payload({"type": "task_done", "summary": summary})
    assert payload is not None
    assert payload["summary"] == summary
    assert decoded_object(payload["summary"])["status"] == "blocked"
    assert not payload.get("summary_truncated", False)


def test_oversized_final_summary_reports_truncation_explicitly() -> None:
    payload = machine_event_payload({"type": "task_done", "summary": "x" * 65_000})
    assert payload is not None
    assert len(str(payload["summary"])) <= 64_000
    assert payload["summary_truncated"] is True


def test_progress_previews_keep_the_existing_small_bound() -> None:
    payload = machine_event_payload({"type": "info", "text": "x" * 2_000})
    assert payload is not None
    assert len(str(payload["text"])) <= 1_000


@pytest.mark.usefixtures("no_external_advisor_models")
def test_real_headless_delivery_keeps_the_complete_final_answer(tmp_path: Path) -> None:
    answer: dict[str, object] = {"reason": "read-only finding " * 150, "status": "blocked"}
    summary = json.dumps(answer)
    rows: list[dict[str, object]] = []

    class Provider:
        name = "Fixture"
        location = "fixture://headless"

        def close(self) -> None:
            pass

        def new_chat(self, timeout: float | None = None) -> None:
            raise AssertionError("fixture execution belongs to agent_run")

        def send(self, text: str, timeout: float | None = None) -> str:
            raise AssertionError("fixture execution belongs to agent_run")

    def run(request: AgentRequest) -> RunResult:
        return RunResult(summary, "done", 1)

    def changes(project: str | Path, tracker: ChangeTracker | None) -> dict[str, object]:
        return {"ok": True, "changed_count": 0, "files": [], "diff": ""}

    def connect(provider_id: str, *, port: int) -> ChatProvider:
        return Provider()

    result = run_headless(
        HeadlessRequest(project=tmp_path / "project", task="diagnose", provider_id="qwen",
                        state_home=tmp_path / "state", project_changes_required=False),
        emit_jsonl=rows.append,
        agent_run=run,
        collect_changes=changes,
        connect_provider=connect,
    )
    assert result.exit_code == 0
    terminal = next(row for row in rows if row["type"] == "task_done")
    assert terminal["summary"] == summary
    assert decoded_object(terminal["summary"])["status"] == "blocked"
