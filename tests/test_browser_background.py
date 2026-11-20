from __future__ import annotations

import threading
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserToolOutput
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.tool_task import ToolTaskInput
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot, offer_browser_tools
from codey.operations.tool_tasks import run_tool_task
from codey.research.browser_search import _search_browser_call
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_browser_production_execution import (
    URL,
    BrowserFixture,
)
from tests.test_browser_production_execution import (
    production_browser as production_browser,
)
from tests.test_tool_task_background import open_runtime

pytestmark = pytest.mark.usefixtures("public_source_dns")


@pytest.mark.parametrize("action", ["click", "reload", "cancel", "url", "timeout", "close", "cancel_close"])
def test_owned_browser_wait_keeps_worker_available_and_original_binding(
    tmp_path: Path, production_browser: BrowserFixture, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    session, tools, browser = production_browser
    runtime = open_runtime(tmp_path)
    snapshot = offer_browser_tools(build_turn_snapshot(session, native=True), session)
    worker: threading.Thread | None = None

    def execute(call: ToolCall, turn: int) -> ToolResult:
        if turn:
            state = operation_from_checkpoint(runtime.app.durable.task("r"), "s")
            assert state is not None
            runtime.app.runtime_mutations.advance_execution("s", "r", expected=state, leaf="writer_running")
        return execute_turn(session, [call], run_id="r", turn=turn, session_id="s", snapshot=snapshot,
            durable=runtime.app.durable, intent_sink=runtime.sink, research_tools=tools,
            managed_outputs=runtime.app.managed_outputs)[0]

    try:
        # Set up the original page through its real worker; navigation is nested
        # CodeMode surface, not an independently exposed model tool.
        nav = browser.navigate(URL)
        assert nav.ok, nav.model_text
        snap = execute(ToolCall("browser_snapshot", {}), 0)
        value = decode_json_object(snap.canonical["value"])
        elements = value["elements"]
        assert isinstance(elements, list)
        ref = next(require_json_string(row["ref"], "ref") for row in elements
                   if isinstance(row, dict) and row.get("role") == "button")
        checked = threading.Event()
        original = browser._check_wait_on_browser_thread

        def check(condition: str, value: str, expected: JsonObject | None) -> BrowserToolOutput:
            result = original(condition, value, expected)
            checked.set()
            return result

        monkeypatch.setattr(browser, "_check_wait_on_browser_thread", check)
        args: JsonObject = {"condition": "url_contains" if action == "url" else "text",
            "value": "?changed" if action == "url" else "37 grams",
            "timeout_ms": 300 if action == "timeout" else 5000, "run_in_background": True}
        accepted = execute(ToolCall("browser_wait", args), 1)
        assert accepted.ok and accepted.kind == "accepted", accepted.model_text
        ref_task = require_json_string(parse_json_object(accepted.model_text)["task_ref"], "task_ref")
        child = runtime.app.durable.claim_task_checkpoint(ref_task)
        assert not ToolTaskInput.from_payload(child.state["tool_input"]).project
        worker = threading.Thread(target=lambda: run_tool_task(runtime.app.durable, child,
            stores=runtime.app.task_submission_stores, signal=threading.Event()))
        worker.start()
        assert checked.wait(timeout=4), runtime.app.durable.task(ref_task)
        if action in {"click", "close"}:
            if action == "close":
                browser.provider.close()  # The admitted task retains the original resource.
                assert browser.provider._session is not None
            assert execute(ToolCall("browser_click", {"ref": ref}), 3).ok
        elif action in {"cancel", "cancel_close"}:
            if action == "cancel_close":
                browser.provider.close()
            runtime.app.durable.request_cancel(ref_task)
        elif action not in {"timeout"}:
            def navigate() -> None:
                page = browser.provider._fetch_page
                assert page is not None
                page.goto(URL + ("?changed" if action == "url" else ""))

            _search_browser_call(navigate)
        worker.join(timeout=6)
        assert not worker.is_alive()
        observed = execute(ToolCall("task_output", {"task_ref": ref_task}), 4)
        repeated = execute(ToolCall("task_output", {"task_ref": ref_task}), 5)
        assert parse_json_object(observed.model_text) == parse_json_object(repeated.model_text)
        payload = parse_json_object(observed.model_text)
        if action in {"click", "url", "close"}:
            assert payload["outcome"] == "succeeded", observed.model_text
        elif action == "reload":
            assert payload["outcome"] == "failed" and "binding changed" in observed.model_text
        elif action == "timeout":
            assert payload["outcome"] == "failed" and "timed out" in observed.model_text
        else:
            assert payload["status"] == "cancelled"
        if action in {"close", "cancel_close"}:
            assert browser.provider._session is None
        else:
            assert browser.snapshot().ok  # Cancelling the wait does not close its page.
        assert browser.provider._leases == 0
        assert not tools.ledger.opened_sources
        assert len(runtime.app.durable.effects(ref_task)) == 1
    finally:
        if worker is not None and worker.is_alive():
            for child in runtime.app.durable.owned_tasks("r"):
                runtime.app.durable.request_cancel(child.task_id)
            worker.join(timeout=8)
        assert runtime.app.close()


@pytest.mark.parametrize("boundary", ["cancel", "lost_resource", "failed_admission", "missing_page"])
def test_browser_binding_cleanup_before_dispatch(
    tmp_path: Path, production_browser: BrowserFixture, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    session, tools, browser = production_browser
    runtime = open_runtime(tmp_path)
    try:
        if boundary != "missing_page":
            assert browser.navigate(URL).ok
        snapshot = offer_browser_tools(build_turn_snapshot(session, native=True), session)
        if boundary == "failed_admission":
            def fail(identity: str, call: ToolCall, *, snapshot: TurnSnapshot,
                     project: str, permission_profile: str, turn: int, tool_index: int,
                     defer_result: bool = False, resource_binding: JsonObject | None = None) -> ToolResult:
                raise OSError("admission storage failure")

            monkeypatch.setattr(runtime.sink, "admit_tool_task", fail)
        call = ToolCall("browser_wait", {"condition": "text", "value": "never", "timeout_ms": 1000,
                                         "run_in_background": True})

        def admit() -> ToolResult:
            return execute_turn(session, [call], run_id="r", turn=0, session_id="s", snapshot=snapshot,
                durable=runtime.app.durable, intent_sink=runtime.sink, research_tools=tools,
                managed_outputs=runtime.app.managed_outputs)[0]

        if boundary == "missing_page":
            rejected = admit()
            assert not rejected.ok and "identified original page" in rejected.model_text
            assert not runtime.app.durable.owned_tasks("r")
        elif boundary == "failed_admission":
            with pytest.raises(OSError, match="admission storage failure"):
                admit()
            assert not runtime.app.durable.owned_tasks("r")
        else:
            accepted = admit()
            assert accepted.ok and accepted.kind == "accepted", accepted.model_text
            ref = require_json_string(parse_json_object(accepted.model_text)["task_ref"], "task_ref")
            if boundary == "cancel":
                browser.provider.close()
                runtime.app.durable.request_cancel("r")
                assert str(runtime.app.durable.task(ref).status) == "cancelled"
                assert browser.provider._session is None
            else:
                # Restart loses volatile handles; the worker must not create a replacement page.
                runtime.app.durable.tool_resources.release_browser(ref)
                child = runtime.app.durable.claim_task_checkpoint(ref)
                before = browser.document_identity()
                run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores,
                              signal=threading.Event())
                assert str(runtime.app.durable.task(ref).status) == "failed"
                assert "original browser resource is unavailable" in str(runtime.app.durable.task(ref).state)
                assert not runtime.app.durable.effects(ref)
                assert browser.document_identity() == before
        assert browser.provider._leases == 0
    finally:
        assert runtime.app.close()
