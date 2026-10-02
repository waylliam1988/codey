"""Canonical edit blocks reach manual consumers; unobserved results are unknown."""

from codey.toolchain.runtime import EditBlock
from tests.manual.impact_guard_ab import _changed_definitions_from_blocks
from tests.manual.real_local_ab import _metrics
from tests.manual.refactor_hint_ab import _candidate_pairs


def test_refactor_hint_consumes_actual_canonical_edit_block():
    assert _candidate_pairs([EditBlock("old_symbol(value)", "new_symbol(value)")]) == [("old_symbol", "new_symbol")]


def test_impact_guard_consumes_actual_canonical_edit_block():
    changes = _changed_definitions_from_blocks("app.py", [EditBlock("def old_symbol():", "def new_symbol():")])
    assert len(changes) == 1
    assert changes[0].old_name == "old_symbol"
    assert changes[0].name == "new_symbol"


def test_ab_tool_start_without_result_is_unknown_not_success():
    metrics = _metrics([{"type": "tool_started", "tool": "edit", "tool_id": "e1"}], [], {"passed": False}, 0)
    assert metrics["successful_tool_calls"] == 0
    assert metrics["failed_tool_calls"] == 0
    assert metrics["unknown_tool_calls"] == 1


def test_ab_anonymous_result_cannot_be_assigned_to_an_unrelated_start():
    metrics = _metrics([
        {"type": "tool_started", "tool": "edit", "tool_id": "e1"},
        {"type": "tool", "tool": "edit", "ok": True},
    ], [], {"passed": False}, 0)
    assert metrics["successful_tool_calls"] == 0
    assert metrics["unknown_tool_calls"] == 1


def test_ab_duplicate_result_events_count_one_observed_call():
    metrics = _metrics([
        {"type": "tool_started", "tool": "edit", "tool_id": "e1"},
        {"type": "tool", "tool": "edit", "tool_id": "e1", "ok": True},
        {"type": "tool_execution_end", "tool": "edit", "tool_id": "e1", "ok": True},
    ], [], {"passed": False}, 0)
    assert metrics["successful_tool_calls"] == 1
    assert metrics["unknown_tool_calls"] == 0
    assert metrics["duplicate_mutation"] is False


def test_ab_verification_truth_is_an_exact_boolean():
    metrics = _metrics([], [], {"passed": "true"}, 0)
    assert metrics["task_success"] is False


def test_ab_malformed_status_is_unknown():
    metrics = _metrics([
        {"type": "tool_started", "tool": "edit", "tool_id": "e1"},
        {"type": "tool", "tool": "edit", "tool_id": "e1", "status": []},
    ], [], {"passed": False}, 0)
    assert metrics["successful_tool_calls"] == 0
    assert metrics["failed_tool_calls"] == 0
    assert metrics["unknown_tool_calls"] == 1


def test_codey_ab_arm_sends_its_case_task(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from tests.manual import real_local_ab as ab

    case = ab.ExperimentCase("specific", "the actual case task", {}, ())
    commands = []
    monkeypatch.setattr(ab.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(stdout="", stderr="", returncode=0)
    ))
    monkeypatch.setattr(ab, "_run_verification", lambda *args, **kwargs: {"passed": True})
    ab._run_arm("codey", tmp_path, tmp_path / "run", "http://127.0.0.1:9/v1", [],
                case=case, baseline_hashes={}, max_turns=2, model_id="model", max_tokens=10)
    assert commands[0][-1] == case.task
