from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.manual.real_local_ab import (
    _content_length,
    _event_rows,
    _metrics,
    _new_project_root,
    _run_verification,
    _safe_json,
    _tool_signature,
    _with_sampling,
    _working_directory,
    _write_pi_config,
)


def test_real_local_ab_projects_are_created_outside_runner_checkout() -> None:
    root = _new_project_root()
    try:
        checkout = Path(__file__).resolve().parents[1]
        assert root != checkout
        assert checkout not in root.parents
        assert not (root / ".git").exists()
    finally:
        import shutil

        shutil.rmtree(root)


def test_real_local_ab_both_arms_execute_in_fixture_directory(tmp_path: Path) -> None:
    assert _working_directory("pi", tmp_path) == tmp_path
    assert _working_directory("codey", tmp_path) == tmp_path


def test_real_local_ab_proxy_parses_http_content_length_header() -> None:
    assert _content_length("17") == 17
    assert _content_length("not-a-length") == 0


def test_real_local_ab_proxy_applies_shared_sampling_budget_to_chat_request() -> None:
    raw = b'{"model":"m","stream":true}'
    rewritten = _with_sampling(raw, 0.0, 2048)
    assert json.loads(rewritten) == {
        "model": "m",
        "stream": True,
        "temperature": 0.0,
        "max_tokens": 2048,
    }


def test_real_local_ab_pi_config_uses_selected_model(tmp_path: Path) -> None:
    config_dir = tmp_path / "pi-config"
    _write_pi_config(config_dir, "http://127.0.0.1:5017", "model-12b")
    payload = json.loads((config_dir / "models.json").read_text(encoding="utf-8"))
    assert payload["providers"]["kobold"]["models"][0]["id"] == "model-12b"


def test_real_local_ab_tool_signature_is_stable_for_duplicate_calls() -> None:
    first = _tool_signature("edit", {"args": {"path": "app.py", "content": "x"}})
    second = _tool_signature("edit", {"args": {"path": "app.py", "content": "x"}})
    assert first == second


def test_real_local_ab_event_parser_ignores_non_json_output() -> None:
    rows = _event_rows("diagnostic\n{" + '"type":"tool_started","tool":"edit"}' + "\n")
    assert rows == [{"type": "tool_started", "tool": "edit"}]


def test_real_local_ab_proxy_extracts_usage_from_sse_response() -> None:
    raw = (
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
        b'data: {"choices":[],"usage":{"total_tokens":123}}\n'
        b"data: [DONE]\n\n"
    )
    parsed = _safe_json(raw)
    assert parsed == {"choices": [], "usage": {"total_tokens": 123}}


def test_real_local_ab_failed_edit_is_not_duplicate_mutation() -> None:
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


def test_real_local_ab_metrics_reject_false_completion_when_verification_fails(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def normalize_name(value): return value\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_fail(): assert False\n", encoding="utf-8")
    verification = _run_verification(tmp_path)
    metrics = _metrics([{"type": "task_done", "stop_reason": "done"}], [], verification, 0)
    assert verification["passed"] is False
    assert metrics["false_completion"] is True
    assert metrics["task_success"] is False


def test_real_local_ab_verifier_does_not_reuse_cached_app_module(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, body in (
        (
            first,
            "import re\n    return re.sub(r\"[^a-z0-9]+\", \"-\", value.lower()).strip(\"-\")",
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

