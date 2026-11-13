"""Upload completion preserves analysis and rejects known unsupported status claims."""
from __future__ import annotations

import json

import pytest

from codey.operations.browser_upload import UploadEvidence
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import _browser_completion_block
from codey.operations.completion_output import prepare_completion_output
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import argument_digest, require_tool_success
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, parse_json_object

URL = "https://example.com/upload"
DOCUMENT: JsonObject = {"url": URL, "loader_id": "loader-1"}
EVIDENCE = UploadEvidence("report.csv", "a" * 64, "", "selection-1", "")


@pytest.mark.parametrize("text", [
    "附件已成功上传，服务器确认接收。报告有 3 条记录。",
    "文件上传成功。", "服务器已确认接收附件。", "服务端已经收到附件。",
    "The attachment was uploaded successfully.", "The server has accepted the attachment.",
    "Upload complete. The report contains 3 entries.",
    "通过网页成功上传了附件。报告有 3 条记录。",
])
def test_known_unsupported_status_assertions_require_a_retry(text: str) -> None:
    issue = EVIDENCE.explanation_issue(text, chinese=True)
    assert "服务器接收状态未核验" in issue
    assert "done" in issue


@pytest.mark.parametrize("text", [
    "已选择附件。报告有 3 条记录，末条缺日期。",
    "服务器接收尚未核验。", "服务器未确认接收。", "没有上传成功。", "尚未上传完成。",
    "无法确认服务器已接收。", "不能证明文件已成功上传。",
    "The server has not accepted the attachment.", "The attachment was not uploaded successfully.",
    "Cannot confirm that the server has accepted the attachment.",
    '页面显示“上传成功”，服务器接收尚未核验。', 'The page says "Upload complete"; acceptance is unverified.',
    "示例：`服务器已接收`。", "```text\nUpload complete\n```",
    "没有成功上传附件。", "未能成功上传附件。", "无法确认附件成功上传。",
])
def test_negation_uncertainty_and_quoted_page_text_are_not_assertions(text: str) -> None:
    assert not EVIDENCE.explanation_issue(text, chinese=False)


def upload_session() -> TaskSession:
    schema = {"type": "object", "properties": {key: {"type": "string"}
              for key in ("explanation", "filename", "sha256", "page_text")},
              "required": ["explanation", "filename", "sha256"], "additionalProperties": False}
    checks = (declaration("json_explanation", json.dumps(schema)),
              require_tool_success("browser_upload", answer_pointer="/filename", result_pointer="/name"),
              require_tool_success("browser_upload", answer_pointer="/sha256", result_pointer="/sha256"))
    session = TaskSession(TaskPolicy(frozenset({"control", "browser.effect"}), required_checks=checks),
                          task_kind="chat", task_text=f"Open {URL} and upload the attachment.",
                          required_source_urls=frozenset({URL}))
    call = ToolCall("browser_upload", {"attachment_id": "att_" + "b" * 64, "ref": "file-1"}, "upload-1")
    session.executed["selection-1"] = ExecutionRecord(name=call.name, ok=True, call_id=call.call_id,
        excerpt="", args_digest=argument_digest(call.args),
        argument_digests={key: argument_digest(value) for key, value in call.args.items()})
    session._memory_results["selection-1"] = ToolResult(call, "selected", ok=True,
        canonical={"value": {"delivery": "verified", "name": EVIDENCE.name, "sha256": EVIDENCE.sha256,
                             "effect_verified": False}}, audit={"browser_document": DOCUMENT})
    return session


@pytest.mark.parametrize("document", [DOCUMENT, None])
def test_assembly_preserves_explanation_even_when_page_evidence_is_missing(document: JsonObject | None) -> None:
    summary = ' \n报告有 3 条记录，末条缺日期。"analysis"\n '
    output = prepare_completion_output(upload_session(), summary, CompletionContext(browser_document=document))
    answer = parse_json_object(output.text)
    assert answer["explanation"] == summary
    assert answer["filename"] == EVIDENCE.name and answer["sha256"] == EVIDENCE.sha256
    assert all(observation.evaluation.passed for observation in output.observations.values())


def test_gate_checks_only_explanation_and_requires_page_evidence_before_status() -> None:
    session = upload_session()
    context = CompletionContext(browser_document=DOCUMENT)
    answer = {"explanation": "已选择附件，服务器接收尚未核验。", "page_text": "上传成功，服务器已接收。"}
    assert _browser_completion_block(session, json.dumps(answer), context) is None
    answer["explanation"] = "服务器已接收。报告有 3 条记录。"
    rejected = _browser_completion_block(session, json.dumps(answer), context)
    assert rejected is not None and not rejected.complete and not rejected.blocked
    assert "Server acceptance was not verified" in rejected.followup
    missing = _browser_completion_block(session, json.dumps(answer), CompletionContext())
    assert missing is not None and "target upload page" in missing.followup
