import base64
import hashlib
from pathlib import Path

import pytest
from playwright.sync_api import Route

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.execution_claims import validate_execution_claims
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_prompt import _result_context, operation_receipts
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_execution import ExecutionDelegate
from codey.runtime.core.models import ToolCall
from codey.storage.input_attachments import InputAttachmentStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.result_schema import validate_output
from codey.toolchain.tool_spec import tool_specs
from tests.test_browser_production_execution import URL
from tests.test_browser_production_execution import production_browser as production_browser


@pytest.mark.parametrize("case", ["allowed", "other_chat", "path", "corrupt"])
def test_upload_only_accepts_verified_user_selected_attachment_bytes(production_browser, tmp_path: Path, case: str) -> None:
    session, tools, browser = production_browser

    def document(route: Route) -> None:
        route.fulfill(content_type="text/html", body='<label>Attachment<input type="file"></label>')

    browser.provider._guard_request = document
    delegate = ExecutionDelegate(session=session, research_tools=tools, session_id="authorized",
                                 managed_outputs=ManagedOutputStore(tmp_path))
    store = InputAttachmentStore(tmp_path, "other" if case == "other_chat" else "authorized")
    selected = store.put("selected.txt", base64.b64encode(b"\xef\xbb\xbfselected bytes\n").decode("ascii"))
    identity = str(selected["id"])
    if case == "corrupt":
        path = store.root / (identity + ".json")
        path.write_text(path.read_text(encoding="utf-8").replace("selected bytes", "corrupt bytes"), encoding="utf-8")
    assert delegate.execute(ToolCall("browser_navigate", {"url": URL}))[1]
    snapshot = delegate.execute(ToolCall("browser_snapshot", {}))[0]
    ref = snapshot.canonical["value"]["elements"][0]["ref"]
    args = {"ref": ref, "attachment_id": str(tmp_path / "secret.txt") if case == "path" else identity}
    result, ok, _ = delegate.execute(ToolCall("browser_upload", args))
    if case != "allowed":
        assert not ok
        return
    assert ok, result.model_text
    value = result.canonical["value"]
    assert value["sha256"] == hashlib.sha256(b"selected bytes\n").hexdigest()
    assert value["bytes"] == len(b"selected bytes\n")
    assert value["attachment_id"] == identity
    assert value["effect_verified"] is False
    validate_output("browser_upload", tool_specs()["browser_upload"].output_schema, value)
    session.executed["upload"] = {"name": "browser_upload", "ok": True}
    session._memory_results["upload"] = result
    assert operation_receipts(session)[-1]["sha256"] == value["sha256"]
    session.last_done_args = {"execution_claims": [{"result_ref": "upload", "status": "success",
                                                   "exit_code_scope": "process"}]}
    with pytest.raises(ValueError, match=r"Eligible shell/process receipts: \[\]"):
        validate_execution_claims(session)
    assert not tools.ledger.read_passages


def test_failed_parent_preserves_the_control_observation_needed_for_repair(production_browser) -> None:
    session, tools, _ = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall("browser_navigate", {"url": URL}))[1]
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="observation", effect_scope="task",
                                         turn=0, research_tools=tools)
    result = execute_turn(session, [ToolCall("codemode", {"code":
        "const s=unwrap(await tools.browser_snapshot({})); return s.elements.find(e=>e.name==='missing').ref;"})],
        executors={"codemode": parent}, snapshot=snapshot, run_id="observation", turn=0, research_tools=tools)[0]
    assert not result.ok
    rendered = _result_context(result, session)
    assert '"name": "City"' in rendered and '"name": "Load"' in rendered
    assert '"ref": "b' in rendered
    assert "Initial article amount" not in rendered
    assert not tools.ledger.read_passages
