"""Full-surface experiment controls, using real Chromium and scripted HTTP replies."""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject
from tools import receipt_bound_edit_surface_probe as probe
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def install_http(monkeypatch: pytest.MonkeyPatch, replies: Iterable[JsonObject]) -> None:
    remaining = iter(replies)

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        reply = next(remaining)
        observe = kwargs.get("observe")
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(reply), "tool_calls": []},
                              "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    monkeypatch.setattr("tools.completion_explanation_ab.service_state", lambda _url: {})


def run_prefetched_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, strict: bool,
                         read_before_upload: bool = False) -> dict[str, object]:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload-test").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    identity = str(attachment["id"])
    read = "return unwrap(await tools.browser_read({}));"
    code = ("const s=unwrap(await tools.browser_snapshot({}));"
            "const ref=s.elements.find(e=>e.name==='Attachment').ref;"
            "unwrap(await tools.browser_upload({ref,attachment_id:inputs.attachment}));"
            "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));")
    code = "unwrap(await tools.browser_read({}));" + code + "return null;" if read_before_upload else code + read
    summary: JsonObject = {"tool": "done", "args": {"summary": "已成功上传，服务器确认接收。" if strict else
                                                    f"文件名：fixture.txt\n内容标记：UPLOAD_BYTES_82\nSHA256：{identity}"}}
    replies: list[JsonObject] = [
        {"tool": "codemode", "args": {"code": code, "inputs": {"attachment": identity}}}, summary,
    ]
    if read_before_upload:
        replies.append({"tool": "codemode", "args": {"code": read}})
    install_http(monkeypatch, [*replies, summary, summary])
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("codey.app.review_service.run_review", lambda *_args, **_kwargs: None)
    return run_case(tmp_path / "run", "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                    max_turns=4, upload_file=True, assembled_upload=strict, upload_navigation_required=strict)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_plain_upload_delivers_receipt_hash_instead_of_model_attachment_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = run_prefetched_upload(tmp_path, monkeypatch, strict=False)
    answer = str(row["answer"])
    assert row["passed"] is True and row["stop_reason"] == "done", row
    assert hashlib.sha256(b"UPLOAD_BYTES_82\n").hexdigest() in answer
    assert "att_" not in answer
    assert row["navigations"] == [URL]
    receipts = tool_receipts(tmp_path / "run" / "state")
    upload, = [receipt.result for receipt in receipts if receipt.result.call.name == "browser_upload"]
    assert upload.ok
    opened, = [receipt.result for receipt in receipts if receipt.result.call.name == "open_url"]
    read, = [receipt.result for receipt in receipts if receipt.result.call.name == "browser_read"]
    assert opened.audit["browser_document"] == upload.audit["browser_document"] == read.audit["browser_document"]
    assert not [receipt for receipt in receipts if receipt.result.call.name == "browser_navigate"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_required_navigation_commits_before_the_first_upload_on_a_prefetched_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = run_prefetched_upload(tmp_path, monkeypatch, strict=True)
    assert row["passed"] is True and row["stop_reason"] == "done", row
    receipts = tool_receipts(tmp_path / "run" / "state")
    steps = [receipt.result for receipt in receipts
             if receipt.result.call.name in {"open_url", "browser_navigate", "browser_upload", "browser_read"}]
    assert [step.call.name for step in steps] == ["open_url", "browser_navigate", "browser_upload", "browser_read"]
    assert all(step.ok for step in steps)
    documents = [step.audit["browser_document"] for step in steps[1:]]
    assert documents[0] == documents[1] == documents[2]
    output = json.loads(str(row["answer"]))
    assert output["sha256"] == hashlib.sha256(b"UPLOAD_BYTES_82\n").hexdigest()
    assert set(output) == {"explanation", "filename", "sha256", "page_text"}
    assert output["filename"] == "fixture.txt" and "UPLOAD_BYTES_82" in output["page_text"]
    assert output["explanation"] == "已在网页选中附件。已读取选择后的页面。服务器接收状态未核验。"


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("strict", [False, True])
def test_upload_report_requires_a_post_selection_read_without_repeating_the_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool,
) -> None:
    row = run_prefetched_upload(tmp_path, monkeypatch, strict=strict, read_before_upload=True)
    assert row["passed"] is True and row["stop_reason"] == "done", row
    rejection = row["first_rejection"]
    assert isinstance(rejection, dict) and "after selecting the attachment" in str(rejection["reason"])
    receipts = tool_receipts(tmp_path / "run" / "state")
    assert [receipt.result.call.name for receipt in receipts
            if receipt.result.call.name in {"browser_read", "browser_upload"}] == ["browser_read", "browser_upload", "browser_read"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("assembled", [False, True])
def test_upload_assembly_uses_receipts_when_model_summary_omits_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, assembled: bool,
) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload-test").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const s=unwrap(await tools.browser_snapshot({}));"
            "const ref=s.elements.find(e=>e.name==='Attachment').ref;"
            "const upload=unwrap(await tools.browser_upload({ref,attachment_id:inputs.attachment}));"
            "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));"
            "const body=unwrap(await tools.browser_read({})); return {upload,body};")
    install_http(monkeypatch, [
        {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL, "attachment": str(attachment["id"])}}},
        {"tool": "done", "args": {"summary": "页面显示已选择的附件。"}},
        {"tool": "done", "args": {"summary": "页面显示已选择的附件。"}},
        {"tool": "done", "args": {"summary": "页面显示已选择的附件。"}},
    ])
    row = probe.run_arm(tmp_path / "run", "upload_bound" if assembled else "upload", arm,
                        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=4)
    assert row.get("stop_reason") == "done", row
    assert row["passed"] is True and row["facts_match"] is True, row
    assert row["receipt_report_available"] is True
    report = json.loads((tmp_path / "run" / "receipt-report.json").read_text(encoding="utf-8"))
    assert report["filename"] == "fixture.txt" and "UPLOAD_BYTES_82" in report["page_text"]
    assert report["effect_verified"] is False
    if assembled:
        output = json.loads(str(row["answer"]))
        assert output["sha256"] == report["sha256"]
        assert output["page_text"] == report["page_text"]
    else:
        assert report["sha256"] in str(row["answer"])
    assert row["model_calls"] == 2


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
def test_fixed_source_attempt_generates_real_403_and_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    install_http(monkeypatch, [{"tool": "done", "args": {"summary": "来源返回 HTTP 403，无法读取正文。"}}])
    row = probe.run_arm(tmp_path / "run", "source_failure_fixed", arm,
                        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=4)
    assert row["passed"] and row["stop_reason"] == "blocked", row
    assert row["source_failure_receipts"] and row["model_calls"] == 1
    request = json.loads((tmp_path / "run" / "request.json").read_text(encoding="utf-8"))
    assert request["required_tools"] == [] and request["sources_open_required"] is True
    assert row["acquisition"] == "scripted-open-url"


def test_snapshot_restore_and_reread_are_separate_scenarios() -> None:
    _, restored = probe.scenario_options("snapshot_restore")
    _, reread = probe.scenario_options("reread")
    assert restored["restore_snapshots"] and not restored["reread"]
    assert reread["reread"] and not reread["restore_snapshots"]


def test_pair_audit_rejects_different_tasks_or_source(tmp_path: Path) -> None:
    for arm in ("A", "B"):
        (tmp_path / arm).mkdir()
        (tmp_path / arm / "task.txt").write_text("same task", encoding="utf-8")
    rows: list[JsonObject] = [{"arm": arm, "passed": True, "source_before": "one", "source_after": "one"}
                              for arm in ("A", "B")]
    assert probe.pair_audit(tmp_path, rows)["comparable"] is True
    rows[1]["source_after"] = "two"
    assert probe.pair_audit(tmp_path, rows)["comparable"] is False
    rows[1]["source_after"] = "one"
    (tmp_path / "B" / "task.txt").write_text("different task", encoding="utf-8")
    assert probe.pair_audit(tmp_path, rows)["comparable"] is False
