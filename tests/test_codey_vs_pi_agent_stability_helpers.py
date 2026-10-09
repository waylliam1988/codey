from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.manual.agent_stability_measurements import tool_signature as _tool_signature
from tests.manual.agent_stability_proxy import safe_json as _safe_json
from tests.manual.codey_vs_pi_agent_stability_ab import (
    TASK_CASES,
    _event_rows,
    _metrics,
    _new_project_root,
    _run_verification,
    _write_pi_config,
)


def test_codey_vs_pi_agent_stability_ab_cases_have_closed_fixture_and_path_contracts() -> None:
    assert "normalize-name" in {case.case_id for case in TASK_CASES}
    for case in TASK_CASES:
        assert case.fixture_files
        assert case.task.strip()
        assert all(path == path.replace("\\", "/") and not path.startswith("../")
                   for path in case.allowed_paths)


def test_codey_vs_pi_agent_stability_ab_verifier_rejects_forbidden_file_changes(tmp_path: Path) -> None:
    case = next(case for case in TASK_CASES if case.case_id == "normalize-name")
    from tests.manual.codey_vs_pi_agent_stability_ab import _fixture, _run_verification, _snapshot_files

    _fixture(tmp_path, case)
    baseline = _snapshot_files(tmp_path)
    (tmp_path / "test_app.py").write_text("changed", encoding="utf-8")
    verification = _run_verification(tmp_path, case=case, baseline_hashes=baseline)
    assert verification["visible_tests"] is False
    assert verification["scope_ok"] is False
    assert verification["passed"] is False


def test_codey_vs_pi_agent_stability_ab_verifier_ignores_runtime_bytecode_artifacts(tmp_path: Path) -> None:
    case = TASK_CASES[0]
    from tests.manual.codey_vs_pi_agent_stability_ab import _fixture, _run_verification, _snapshot_files

    _fixture(tmp_path, case)
    baseline = _snapshot_files(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"runtime")
    verification = _run_verification(tmp_path, case=case, baseline_hashes=baseline)
    assert verification["scope_ok"] is True


def test_codey_vs_pi_agent_stability_ab_trace_analyzer_reports_tool_and_usage_metrics() -> None:
    rows = [
        {"type": "tool_started", "tool": "read_file", "tool_id": "r1"},
        {"type": "tool", "tool": "read_file", "tool_id": "r1", "ok": False},
        {"type": "tool_started", "tool": "edit", "tool_id": "e1", "args": {"path": "app.py"}},
        {"type": "tool", "tool": "edit", "tool_id": "e1", "ok": True, "changed": True},
        {"type": "task_done", "stop_reason": "done"},
    ]
    records = [{"path": "/v1/chat/completions", "response": {
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}}]
    metrics = _metrics(rows, records, {"passed": True}, 0)
    assert metrics["failed_tool_calls"] == 1
    assert metrics["read_calls"] == 1
    assert metrics["edit_calls"] == 1
    assert metrics["llm_rounds"] == 1
    assert metrics["input_tokens"] == 10
    assert metrics["output_tokens"] == 4


def test_codey_vs_pi_agent_stability_ab_trace_analyzer_counts_each_tool_id_once() -> None:
    rows = [
        {"type": "tool_started", "tool": "edit", "tool_id": "e1"},
        {"type": "tool", "tool": "edit", "tool_id": "e1", "ok": True, "changed": True},
        {"type": "tool_finished", "tool": "edit", "tool_id": "e1", "ok": True},
    ]
    metrics = _metrics(rows, [], {"passed": True}, 0)
    assert metrics["edit_calls"] == 1
    assert metrics["successful_tool_calls"] == 1
    assert metrics["failed_tool_calls"] == 0


def test_codey_vs_pi_agent_stability_ab_projects_are_created_outside_runner_checkout() -> None:
    root = _new_project_root()
    try:
        checkout = Path(__file__).resolve().parents[1]
        assert root != checkout
        assert checkout not in root.parents
        assert not (root / ".git").exists()
    finally:
        import shutil

        shutil.rmtree(root)


def test_codey_vs_pi_agent_stability_ab_pi_config_uses_selected_model(tmp_path: Path) -> None:
    config_dir = tmp_path / "pi-config"
    _write_pi_config(config_dir, "http://127.0.0.1:5017", "model-12b")
    payload = json.loads((config_dir / "models.json").read_text(encoding="utf-8"))
    assert payload["providers"]["kobold"]["models"][0]["id"] == "model-12b"


def test_codey_vs_pi_agent_stability_ab_tool_signature_is_stable_for_duplicate_calls() -> None:
    first = _tool_signature("edit", {"args": {"path": "app.py", "content": "x"}})
    second = _tool_signature("edit", {"args": {"path": "app.py", "content": "x"}})
    assert first == second


def test_codey_vs_pi_agent_stability_ab_event_parser_ignores_non_json_output() -> None:
    rows = _event_rows("diagnostic\n{" + '"type":"tool_started","tool":"edit"}' + "\n")
    assert rows == [{"type": "tool_started", "tool": "edit"}]


def test_codey_vs_pi_agent_stability_ab_proxy_extracts_usage_from_sse_response() -> None:
    raw = (
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
        b'data: {"choices":[],"usage":{"total_tokens":123}}\n'
        b"data: [DONE]\n\n"
    )
    parsed = _safe_json(raw)
    assert parsed == {"choices": [], "usage": {"total_tokens": 123}}


def test_codey_vs_pi_agent_stability_ab_failed_edit_is_not_duplicate_mutation() -> None:
    rows = [
        {
            "type": "tool_started",
            "tool": "edit",
            "tool_id": "3:0",
            "args": {"path": "app.py", "content": "bad"},
        },
        {
            "type": "tool",
            "tool": "edit",
            "tool_id": "3:0",
            "ok": False,
        },
        {
            "type": "tool_started",
            "tool": "edit",
            "tool_id": "4:0",
            "args": {"path": "app.py", "content": "good"},
        },
        {
            "type": "tool",
            "tool": "edit",
            "tool_id": "4:0",
            "ok": True,
            "changed": True,
        },
    ]
    verification = {"passed": True}
    metrics = _metrics(rows, [], verification, 0)
    assert metrics["mutation_calls"] == 1
    assert metrics["duplicate_mutation"] is False


def test_codey_vs_pi_agent_stability_ab_metrics_reject_false_completion_when_verification_fails(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def normalize_name(value): return value\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_fail(): assert False\n", encoding="utf-8")
    verification = _run_verification(tmp_path)
    metrics = _metrics([{"type": "task_done", "stop_reason": "done"}], [], verification, 0)
    assert verification["passed"] is False
    assert metrics["false_completion"] is True
    assert metrics["task_success"] is False


def test_codey_vs_pi_agent_stability_ab_verifier_does_not_reuse_cached_app_module(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, body in (
        (
            first,
            "import re\n    return \"-\".join(re.sub(r\"[^a-z0-9\\s]\", \"\", value.lower()).split())",
        ),
        (second, "return \"wrong\""),
    ):
        root.mkdir()
        (root / "app.py").write_text(
            f"def normalize_name(value: str) -> str:\n    {body}\n",
            encoding="utf-8",
        )
        (root / "test_app.py").write_text(
            "from app import normalize_name\n\n"
            "import unittest\n\n"
            "class NameTests(unittest.TestCase):\n"
            "    def test_trim_and_collapse(self):\n"
            "        self.assertEqual(normalize_name('  Hello   World  '), 'hello-world')\n\n"
            "    def test_punctuation(self):\n"
            "        self.assertEqual(normalize_name(' Hello, World! '), 'hello-world')\n",
            encoding="utf-8",
        )
    sys.modules.pop("app", None)
    assert _run_verification(first)["passed"] is True
    assert _run_verification(second)["passed"] is False

