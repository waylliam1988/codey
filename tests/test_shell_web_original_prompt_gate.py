"""Original-prompt probes cannot certify answer semantics from tool activity."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_shell_web_food_gate import evidence
from tools.shell_web_original_prompt_gate import TASKS, analyze


def test_original_food_probe_records_evidence_without_semantic_pass(tmp_path: Path) -> None:
    evidence(tmp_path)
    report = analyze(tmp_path, "food")
    assert report["acquisition_ok"] is True
    assert report["answer_review_required"] is True and "ok" not in report
    assert report["task"] == "你帮我上网看看福州有什么好吃的"


def test_successful_open_without_delivered_body_does_not_pass(tmp_path: Path) -> None:
    evidence(tmp_path)
    (tmp_path / "provider.jsonl").write_text("", encoding="utf-8")
    assert analyze(tmp_path, "food")["acquisition_ok"] is False


def test_blocked_site_does_not_certify_unsupported_summary(tmp_path: Path) -> None:
    events = [
        {"type": "tool", "tool_name": "open_url", "ok": False, "path": "https://api.lihe.chat/"},
        {"type": "task_done", "stop_reason": "blocked", "summary": "unsupported claim"},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
    (tmp_path / "provider.jsonl").write_text("", encoding="utf-8")
    report = analyze(tmp_path, "site")
    assert report["acquisition_ok"] is True and report["answer_review_required"] is True
    assert report["summary"] == "unsupported claim" and report["task"] == TASKS["site"]
