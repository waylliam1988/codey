"""Keep the diagnostic runner from turning missing coverage into green checks."""
from __future__ import annotations

import json

import pytest

from tools.shell_web_diagnostic_gate import (
    analyze_web_artifacts,
    current_receipt,
    exact_fields,
    page_fixture,
    parse_json_reply,
    receipt_delivery_audit,
    replay_jobs,
    summarize,
)


def test_exact_fields_preserves_boolean_integer_distinction():
    assert exact_fields({"exit_code": 0, "verified": False}, {"exit_code": 0, "verified": False})
    assert not exact_fields({"exit_code": False}, {"exit_code": 0})
    assert not exact_fields({}, {"exit_code": None})


@pytest.mark.parametrize("reply", ['prefix {"ok":true}', '[]', 'null', '{"ok":true} suffix'])
def test_parser_rejects_prose_and_non_objects(reply):
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_json_reply(reply)


def test_parser_accepts_explicit_json_fence():
    assert parse_json_reply('```json\n{"ok":true}\n```') == {"ok": True}


def test_fixture_cannot_reveal_title_in_first_20k():
    from codey.research.extract import extract_title

    html = page_fixture("unique-marker")
    assert html.index("<title>") > 100_000
    assert not extract_title(html[:20_000])
    assert extract_title(html) == "Fixture Gateway unique-marker"


def test_current_receipt_is_production_formatter_not_hardcoded_legacy_copy():
    receipt = current_receipt("DIAGNOSTIC_OUTPUT", code=23)
    assert "DIAGNOSTIC_OUTPUT" in receipt
    assert "result_probe" in receipt


def test_replays_distinguish_missing_information_from_model_failure():
    jobs = replay_jobs()
    assert len(jobs) == 8
    failure = next(job for job in jobs if job["case"] == "failure_receipt" and job["variant"] == "current")
    assert failure["expected"]["exit_code"] is None
    assert failure["expected"]["cause"] == "unknown"
    assert not failure["information_available"]


def test_preflight_failure_does_not_count_as_model_success():
    report = summarize([{"scope": "model_preflight", "model_label": "solar", "ok": False}],
                       scopes=["replay"], models=["solar"], repeat=1)
    assert not report["ok"]
    assert report["blocked_models"] == ["solar"]
    assert report["completed_replays"] == 0
    assert report["expected_replays"] == 8


def test_missing_e2e_is_not_vacuous_success():
    report = summarize([{"scope": "model_preflight", "model_label": "gemma", "ok": True}],
                       scopes=["e2e"], models=["gemma"], repeat=1, shells=["cmd"])
    assert not report["ok"]
    assert report["expected_e2e"] == 1
    assert report["completed_e2e"] == 0


def test_receipt_audit_distinguishes_first_delivery_and_later_recovery(tmp_path):
    messages = [
        {"role": "tool", "content": 'SHELL_GATE_RESULT\nResult metadata:\n{"tool":"shell","ok":true}'},
        {"role": "tool", "content": '{"exit_code":7,"text":"SHELL_GATE_RESULT"}'},
    ]
    (tmp_path / "provider.jsonl").write_text(json.dumps({"type": "request", "payload": {"messages": messages}}),
                                              encoding="utf-8")
    audit = receipt_delivery_audit(tmp_path)
    assert audit["receipt_seen"]
    assert not audit["initial_exit_code_visible"]
    assert audit["eventual_exit_code_visible"]


def test_compatibility_receipt_does_not_count_tool_docs_as_observed_exit(tmp_path):
    content = ('Tool documentation: {"exit_code":0}\n'
               '[result: shell]\nSHELL_GATE_RESULT\nResult metadata:\n{"tool":"shell","ok":true}\n\n'
               '[result: read_tool_result]\n{"exit_code":7}')
    row = {"type": "request", "payload": {"messages": [{"role": "user", "content": content}]}}
    (tmp_path / "provider.jsonl").write_text(json.dumps(row), encoding="utf-8")
    audit = receipt_delivery_audit(tmp_path)
    assert audit["receipt_seen"]
    assert not audit["initial_exit_code_visible"]
    assert audit["eventual_exit_code_visible"]


def test_duplicate_slot_cannot_replace_missing_case():
    row = {"scope": "production_http_approval", "model_label": "gemma", "repetition": 1,
           "case": "approval", "variant": "cmd", "ok": True}
    report = summarize([row, row], scopes=["e2e"], models=["gemma"], repeat=1)
    assert not report["ok"]
    assert not report["matrix_complete"]
    assert report["missing_slots"]
    assert report["duplicate_slots"]


def test_missing_live_web_never_passes():
    report = summarize([{"ok": True}], scopes=["web"], models=["solar"], repeat=1)
    assert not report["ok"]
    assert report["expected_web"] == 2


@pytest.mark.parametrize("stop_reason", ["stopped", "max_turns"])
def test_runtime_terminal_is_not_misclassified_as_model_json_error(tmp_path, stop_reason):
    rows = [
        {"type": "tool", "tool_name": "open_url", "ok": False,
         "result": "SKIPPED: low_value_landing_page_url"},
        {"type": "task_done", "stop_reason": stop_reason, "summary": "max turns reached"},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    (tmp_path / "provider.jsonl").write_text("", encoding="utf-8")
    facts = analyze_web_artifacts(tmp_path, {"url": "https://example.test/"})
    assert not facts["ok"]
    assert facts["failure_stage"] == "source_filter_blocked"
    assert facts["summary_format_error"] == ""
    assert facts["web_tool_available"]


def test_headless_denial_is_visible_without_inventing_model_failure(tmp_path):
    rows = [{"type": "shell_rejected", "reason": "headless_default_deny"},
            {"type": "task_done", "stop_reason": "stopped", "summary": "stopped"}]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    (tmp_path / "provider.jsonl").write_text("", encoding="utf-8")
    facts = analyze_web_artifacts(tmp_path, {"url": "https://example.test/"})
    assert facts["headless_shell_denied"]
    assert facts["failure_stage"] == "web_capability_absent"
    assert facts["summary_format_error"] == ""
