"""Declared upload waiting runs through real production child receipts."""
from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.operations.browser_upload import structured_upload_spec, task_upload_scope
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.storage.input_attachments import InputAttachmentStore
from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec
from codey.types.json_value import JsonObject
from tests.test_browser_structured_upload_production import action
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import HTML, URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


@pytest.mark.parametrize("condition", ["none", "missing", "declared"])
def test_read_contract_requires_model_condition_only_when_task_has_none(condition: str) -> None:
    checks = (() if condition == "none" else (require_tool_success("browser_wait", arguments=(
        {"condition": "text", "value": "UPLOAD_BYTES_82"} if condition == "declared" else {})),))
    policy = TaskPolicy(frozenset({"control", "browser.effect"}), required_checks=checks)
    scope = task_upload_scope("chat", f"Upload the attachment at {URL}.", frozenset({URL}), policy)
    assert scope is not None
    spec = spec_for_tool("codemode")
    assert spec is not None
    model = structured_upload_spec(spec, scope, "read")
    assert bool(validate_args_with_spec(model, {"upload": {"action": "read"}})) is (condition == "missing")
    assert not validate_args_with_spec(model, {"upload": {"action": "read", "wait_for_text": "UPLOAD_BYTES_82"}})


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("case", ["declared", "supplied", "timeout", "direct", "partial", "partial_timeout"])
def test_upload_read_binds_task_wait_and_stops_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload-wait-test").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode("ascii"))

    def replies() -> Iterator[JsonObject]:
        yield action("observe")
        yield action("select", attachment_id=str(attachment["id"]), target={"name": "Attachment"})
        marker = "processed" if case in {"partial", "partial_timeout"} else "UPLOAD_BYTES_82"
        yield action("read", **({"wait_for_text": marker} if case in {"supplied", "partial", "partial_timeout"} else {}))
        yield {"tool": "done", "args": {"summary": "已选择附件并读取页面。"}}

    install_http(monkeypatch, replies())

    def no_tabs(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    def no_review(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", no_tabs)
    monkeypatch.setattr("codey.app.review_service.run_review", no_review)
    html = HTML.replace("this.files[0].name+':'+t", "'Not ready'") if case == "timeout" else HTML
    if case == "partial":
        html = HTML.replace("this.files[0].name+':'+t", "this.files[0].name+':'+t+' processed'")
    extra = (require_tool_success("browser_wait", via="codemode", arguments={"value": "processed"}),
             ) if case in {"partial", "partial_timeout"} else ()
    directory = tmp_path / "run"
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                      upload_file=True, explicit_wait=case != "direct", fixture_html=html, max_turns=4,
                      restore_snapshots=True, additional_required_tools=extra)
    receipts = [row.result for row in tool_receipts(directory / "state")]
    waits = [row for row in receipts if row.call.name == "browser_wait"]
    reads = [row for row in receipts if row.call.name == "browser_read"]
    assert len(waits) == (0 if case == "direct" else 2 if extra else 1), result
    if case in {"timeout", "partial_timeout"}:
        assert not waits[-1].ok and not reads
        if extra:
            assert waits[0].ok
        assert result["passed"] is False and result["stop_reason"] != "done"
    else:
        assert result["passed"] is True, result
        assert len(reads) == 1 and reads[0].ok
        if waits:
            assert waits[0].ok and waits[0].call.args["value"] == "UPLOAD_BYTES_82"
            assert receipts.index(waits[0]) < receipts.index(reads[0])
