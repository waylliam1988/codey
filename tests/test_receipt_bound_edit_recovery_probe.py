"""Exercise recovery scoring and the real headless path without model inference."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_ERROR
from codey.types.json_value import JsonObject, decode_json_object
from tools import receipt_bound_edit_ab as ab
from tools import receipt_bound_edit_recovery_probe as probe
from tools.local_model_gate_attempts import GateTarget


@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("scenario", ["clean", "invalid-read", "stale-recovery"])
def test_fixed_program_safety_and_recovery(tmp_path: Path, arm: str, scenario: str) -> None:
    directory = tmp_path / "probe"
    row = probe.run_deterministic_case(directory, arm, scenario)
    assert row["passed"], row
    audit = json.loads((directory / "receipts.json").read_text(encoding="utf-8"))
    stages = json.loads((directory / "stages.json").read_text(encoding="utf-8"))
    if scenario == "stale-recovery":
        assert row["read_calls"] == row["edit_calls"] == 2
        assert audit["true_stale_conflicts"] == 1 and audit["fresh_read_recovery"] is True
        assert audit["first_edit_rejection"]["valid_original_read"] is True
        assert stages[0]["ok"] is False and stages[1]["ok"] is True
        assert stages[1]["file"]["external_revision"] == probe.EXTERNAL_FIELD
        assert audit["stale_read_refs"][0] != audit["successful_edit_refs"][0]
    elif scenario == "invalid-read":
        assert audit["true_stale_conflicts"] == 0
        assert audit["first_tool_rejection"] is not None


def read_receipt(path: str = "result.json", *, complete: bool = True) -> ToolResult:
    return ToolResult(ToolCall("read_file", {"path": path}), "old text", ok=True,
                      canonical={"value": {"text": "old text", "text_complete": complete}})


def edit_receipt(ref: str | None, *, ok: bool = False) -> ToolResult:
    args: JsonObject = {"path": "result.json", "content": "updated"}
    if ref is not None:
        args["base_result_ref"] = ref
    return ToolResult(ToolCall("edit", args), "updated" if ok else STALE_READ_REFERENCE_ERROR, ok=ok)


@pytest.mark.parametrize("defect", ["missing", "unknown", "partial", "other-path", "future-read"])
def test_arbitrary_edit_failure_never_counts_as_a_stale_conflict(defect: str) -> None:
    read = read_receipt("another.json" if defect == "other-path" else "result.json", complete=defect != "partial")
    ref = None if defect == "missing" else "invented" if defect == "unknown" else "read-1"
    rows = [("read-1", read), ("edit-1", edit_receipt(ref))]
    if defect == "future-read":
        rows.reverse()
    audit = probe.receipt_audit(rows)
    assert audit["true_stale_conflicts"] == 0
    assert audit["fresh_read_recovery"] is False


def test_recovery_needs_a_new_read_after_the_conflict() -> None:
    rows = [("read-1", read_receipt()), ("read-2", read_receipt()),
            ("edit-1", edit_receipt("read-1")), ("edit-2", edit_receipt("read-2", ok=True))]
    assert probe.receipt_audit(rows)["fresh_read_recovery"] is False
    rows.append(("read-3", read_receipt()))
    rows.append(("edit-3", edit_receipt("read-3", ok=True)))
    assert probe.receipt_audit(rows)["fresh_read_recovery"] is True


@pytest.mark.usefixtures("scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("bad_first_attempt", [False, True])
def test_live_harness_injects_after_committed_read_and_scores_exact_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, bad_first_attempt: bool,
) -> None:
    """Scripted HTTP replies, real provider/headless/guest/edit/durable receipts."""
    replies: list[JsonObject] = [
        {"tool": "codemode", "args": {"code": probe.fixed_program(arm, invalid_read=bad_first_attempt)}},
        {"tool": "codemode", "args": {"code": probe.fixed_program(arm)}},
        {"tool": "done", "args": {"summary": "已更新 result.json，保留其他字段及备份。"}},
    ]
    calls = 0

    def no_browser(*args: object, **kwargs: object) -> None:
        pytest.fail("Edit probe must not connect a browser model")

    monkeypatch.setattr("codey.automation.browser.open_chat_page", no_browser)

    def scripted_http(url: str, payload: dict[str, object], headers: dict[str, str],
                      **kwargs: object) -> dict[str, object]:
        nonlocal calls
        assert calls < len(replies), "Unexpected extra provider call"
        reply = replies[calls]
        calls += 1
        observe = kwargs.get("observe")
        assert callable(observe)
        wire = json.dumps(payload).encode("utf-8")
        observe(attempt=1, data=wire, phase="request", response_bytes=0, seconds=0.0)
        message: dict[str, object] = {"role": "assistant", "content": json.dumps(reply), "tool_calls": []}
        if payload.get("tools"):
            message.update(content="", tool_calls=[{
                "id": f"call-{calls}", "type": "function", "function": {
                    "name": reply["tool"], "arguments": json.dumps(reply["args"])},
            }])
        return {"choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted_http)
    target = GateTarget("http://scripted.test/v1", "scripted-fixture", 32768, 8192, 12000)
    directory = tmp_path / arm
    row = probe.run_live_arm(directory, ab.CASES[0], arm, target, max_turns=5)
    assert row.get("stop_reason") == "done", row
    assert row["target_correct"] and row["backup_preserved"] and row["injection_occurred"], row
    assert row["passed"] is (not bad_first_attempt), row
    assert row["true_stale_conflicts"] == (0 if bad_first_attempt else 1)
    assert (directory / "task.txt").read_text(encoding="utf-8") == ab.common_task(ab.CASES[0])
    assert calls == 3
    assert row["model_calls"] == row["model_responses"] == 3
    audit = json.loads((directory / "receipts.json").read_text(encoding="utf-8"))
    assert audit["first_tool_rejection"] is not None


def test_offline_default_never_selects_a_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_model(*args: object, **kwargs: object) -> None:
        pytest.fail("Offline mode must not discover or invoke a provider")

    monkeypatch.setattr(probe, "select_target", no_model)
    monkeypatch.setattr(probe, "run_deterministic_case", lambda *args: {"passed": True})
    monkeypatch.setattr("sys.argv", ["probe", "--run-dir", str(tmp_path / "offline")])
    assert probe.main() == 0
    summary = json.loads((tmp_path / "offline" / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "fixed-program" and summary["total_runs"] == 6


def test_source_change_invalidates_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digests = iter(["before", "after"])
    monkeypatch.setattr(ab, "production_source_digest", lambda: next(digests))
    monkeypatch.setattr(probe, "run_deterministic_case", lambda *args: {"passed": True})
    monkeypatch.setattr("sys.argv", ["probe", "--run-dir", str(tmp_path / "changed")])
    assert probe.main() == 1
    summary = decode_json_object(json.loads((tmp_path / "changed" / "summary.json").read_text(encoding="utf-8")))
    assert summary["passed_runs"] == 6 and summary["source_stable"] is False and summary["passed"] is False
