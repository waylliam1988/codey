"""Cross-language protocol fixtures consumed by Python and Vitest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codey.agents.shell_approval import shell_command_event_fields
from codey.app.event_payloads import machine_event_payload


FIXTURES = Path(__file__).parent / "fixtures" / "protocol"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_task_done_and_research_fixtures_match_machine_event_contract() -> None:
    task_done = load_fixture("task_done.json")
    projected = machine_event_payload(task_done)
    assert projected is not None
    assert projected["type"] == "task_done"
    assert projected["summary"] == task_done["summary"]
    assert projected["stop_reason"] == "done"

    research = load_fixture("research.json")
    for mode in ("pure", "hybrid"):
        event = research[mode]
        projected = machine_event_payload(event)
        assert projected is not None
        assert projected["type"] == "task_done"
        assert event["mode"] == ("research" if mode == "pure" else "hybrid")
        assert event["research"]["restoreable"] in (True, False)


def test_approval_fixture_has_current_command_binding_and_shell_result_shape() -> None:
    approval = load_fixture("approval.json")
    fields = shell_command_event_fields(approval)
    assert fields["command"] == approval["command"]
    assert fields["command_sha256"] == hashlib.sha256(approval["command"].encode()).hexdigest()
    assert fields["command_chars"] == len(approval["command"])

    result = load_fixture("shell_result.json")
    assert result["type"] == "shell_result"
    assert result["approved"] is True
    assert result["exit_code"] == 0
    assert result["command_sha256"] == fields["command_sha256"]


def test_run_state_and_changes_fixtures_preserve_protocol_variants() -> None:
    state = load_fixture("run_state.json")
    assert state["busy"] is True
    assert state["pending_event"]["type"] == "shell_request"
    assert state["research_restore_runs"] == ["run-research-pure-1"]

    changes = load_fixture("changes.json")
    assert changes["list"]["files"][0]["preview_status"] == "text"
    assert changes["truncated"]["truncated"] is True
    assert changes["file_preview"] == {
        "ok": True,
        "path": "bundle.js",
        "status": "too_large",
        "size": 184324,
        "truncated": True,
    }
