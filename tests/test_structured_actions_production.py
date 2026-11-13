"""Real browser and version conflicts with fixed replies entering the action surface."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.policies.tool_requirement import require_structured_actions
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_receipt_bound_edit_surface_probe import install_http
from tests.test_structured_actions import action
from tools.browser_production_live_gate import DOWNLOAD_BYTES, HTML, URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target
from tools.structured_actions_live_gate import bindings, checks, run, schema, task

TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("mode", ["chat", "hybrid"])
def test_action_flow_restores_contract_and_repairs_only_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    replies = [action("navigate", target="task"), action("snapshot"),
        action("fill", target={"role": "textbox", "name": "Locality"}, value="福州"),
        action("click", target={"role": "button", "name": "Load record"}),
        action("wait", condition="text", value="鱼丸", timeout_ms=5000), action("read"),
        action("eval", code="window.__contractToken"), action("tabs")]
    if mode == "hybrid":
        edit = action("edit_json", path="result.json", changes={"amount": 173, "code": "QUARTZ_851"})
        replies.extend([edit, edit])
    corrected = "已填写并加载当前记录。\n\n净重为 173 克，校验码 QUARTZ_851。"
    drafts = ["已完成。", corrected + " SCHEMA_TOKEN_74", corrected + " HTTPS://example.com/production_browser_record", corrected]
    replies.extend({"tool": "done", "args": {"summary": draft}} for draft in drafts)
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = run(directory, mode, TARGET)
    assert row["passed"] is True and row["stop_reason"] == "done", row


    assert parse_json_object(str(row["answer"]))["explanation"] == corrected
    receipts = tool_receipts(directory / "state")
    assert sum(item.result.call.name == "browser_read" for item in receipts) == 1
    assert sum(item.result.call.name == "browser_click" for item in receipts) == 1
    assert row["restored_snapshots"]
    attempts = row["repair_attempts"]
    assert isinstance(attempts, list) and len(attempts) == 3
    assert all(draft in str(decode_json_object(attempts[index])["reason"]) for index, draft in enumerate(drafts[:-1]))
    assert "answer_text_missing" in str(attempts[0])
    assert "answer_text_forbidden" in str(attempts[1])
    assert "output_format_invalid" in str(attempts[2])
    if mode == "hybrid":
        reads = [item.identity for item in receipts if item.result.call.name == "read_file"]
        edits = [item.result for item in receipts if item.result.call.name == "edit"]
        assert len(reads) == len(edits) == 2
        assert [edit.call.args["base_result_ref"] for edit in edits] == reads
        assert not edits[0].ok and edits[1].ok
        assert row["version_check_reached"] is True and row["conflict_recovery_covered"] is True
        assert "external_revision" in parse_json_object((directory / "project-result.json").read_text(encoding="utf-8"))


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_fixed_download_action_uses_existing_managed_output_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected_hash = hashlib.sha256(DOWNLOAD_BYTES).hexdigest()
    install_http(monkeypatch, [action("navigate", target="task"),
        action("download", target={"role": "link", "name": "Download fixture"}), action("read"),
        {"tool": "done", "args": {"summary": f"fixture-download.txt; {len(DOWNLOAD_BYTES)} bytes; SHA256 {expected_hash}"}}])
    directory = tmp_path / "run"
    row = run_case(directory, "chat", TARGET, download_file=True, max_turns=4, restore_snapshots=True,
        additional_required_tools=(require_structured_actions(),),
        task_transform=lambda _: f"Open <{URL}>. Download the named link Download fixture and read the page; report the receipt.")
    assert row["passed"] is True, row
    downloads = [item.result for item in tool_receipts(directory / "state") if item.result.call.name == "browser_download"]
    assert len(downloads) == 1 and downloads[0].ok


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("case", ["stale", "duplicate"])
def test_real_control_ambiguity_and_stale_refs_require_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    directory = tmp_path / "run"

    def button_ref() -> str:
        snapshots = [item.result for item in tool_receipts(directory / "state") if item.result.call.name == "browser_snapshot"]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(require_json_string(row["ref"], "ref") for item in elements
                    if (row := decode_json_object(item))["name"] == "Load record")

    def replies() -> Iterator[JsonObject]:
        yield action("navigate", target="task")
        yield action("snapshot")
        old_ref = button_ref()
        yield action("fill", target={"role": "textbox", "name": "Locality"}, value="福州")
        if case == "stale":
            yield action("click", target={"ref": old_ref})
            click = next(item.result for item in reversed(tool_receipts(directory / "state")) if item.result.call.name == "browser_click")
            value = decode_json_object(click.canonical["value"])
            assert not click.ok and value["reason"] == "stale_ref" and value["delivery"] == "not_delivered"
            yield action("click", target={"role": "button", "name": "Load record"})
        else:
            yield action("click", target={"role": "button", "name": "Load record"})
            before = tool_receipts(directory / "state")
            assert not any(item.result.call.name == "browser_click" for item in before)
            count = sum(item.result.call.name == "browser_snapshot" for item in before)
            ref = button_ref()
            parent = next(item.result for item in reversed(before) if item.result.call.name == "codemode")
            assert not parent.ok and ref in parent.model_text
            yield action("click", target={"ref": ref})
            assert sum(item.result.call.name == "browser_snapshot" for item in tool_receipts(directory / "state")) == count
        yield action("wait", condition="text", value="鱼丸", timeout_ms=5000)
        yield action("read")
        yield action("eval", code="window.__contractToken")
        yield action("tabs")
        yield {"tool": "done", "args": {"summary": "已纠正控件选择并读取当前页面。\n\n净重173克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    html = HTML.replace("<article>", "<button>Load record</button><article>") if case == "duplicate" else HTML
    row = run_case(directory, "chat", TARGET, explanation_output=True, restore_snapshots=True,
        fixture_html=html, max_turns=13, additional_required_tools=checks(), explanation_bindings=bindings(),
        explanation_schema=schema(), task_transform=lambda _: task("chat"))
    assert row["passed"] is True and row["stop_reason"] == "done", row
