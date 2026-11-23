from __future__ import annotations

import threading
import time

import pytest

from codey.codemode.catalog import (
    MODEL_CATALOG_BUDGET_BYTES,
    build_catalog_snapshot,
    render_declarations,
    snapshot_catalog,
)
from codey.codemode.runtime import CodeModeHandle, CodeModeOutcome, CodeModeRequest, CodeModeSession, NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry
from codey.operations.kernel_prompt import _text_request_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core import cancellation
from codey.types.json_value import decode_json_object


def test_catalog_discovery_uses_only_frozen_entries_and_never_invokes_tools() -> None:
    catalog = build_catalog_snapshot((
        ToolCatalogEntry("read_file", "Read a file", {"type": "object"}, namespace="project"),
        ToolCatalogEntry("find_references", "Find symbol references", {"type": "object"}, namespace="project"),
    ))
    invoked: list[str] = []

    def invoke(name: str, _args: dict[str, object]) -> object:
        invoked.append(name)
        return None

    result = NodeWorkerRuntime().execute(
        "const matches=await search('references'); return {matches, missing:await describe('guessed')};",
        catalog=catalog.entries, invoke=invoke,
    )
    assert result.ok, result.error
    value = decode_json_object(result.value)
    matches = decode_json_object(value["matches"])["items"]
    assert isinstance(matches, list) and isinstance(matches[0], dict)
    assert matches[0]["path"] == "tools.find_references"
    assert value["missing"] is None and not invoked


def test_catalog_summary_is_derived_from_frozen_catalog() -> None:
    catalog = build_catalog_snapshot(tuple(
        ToolCatalogEntry(f"read_{index}", "Read file", namespace="project") for index in range(20)
    ))
    summary = catalog.summary(budget=30)
    assert summary.total == 20 and summary.shown < summary.total
    assert summary.namespaces[0].name == "project" and summary.namespaces[0].count == 20
    page = catalog.search("read", limit=2)
    assert len(page.items) == 2 and page.remaining == 18 and page.next_offset == 2
    assert catalog.describe(page.items[0].path) == page.items[0]


def test_model_catalog_budget_keeps_pinned_tools_and_discovery_retains_parameter_docs() -> None:
    entries = tuple(ToolCatalogEntry(f"tool_{index}", "说明" * 1_000,
                                    {"type": "object", "properties": {
                                        "path": {"type": "string", "description": "Observed exact path"}}},
                                    namespace="project") for index in range(50))
    catalog = build_catalog_snapshot((*entries, ToolCatalogEntry("read_file", "read", namespace="project")))
    prompt = render_declarations(catalog)
    assert len(prompt.encode("utf-8")) <= MODEL_CATALOG_BUDGET_BYTES
    assert "read_file(args:" in prompt and "project: 51 tools" in prompt
    omitted = next(entry for entry in entries if entry.name + "(args:" not in prompt)
    item = catalog.describe("tools." + omitted.name)
    assert item is not None and "Observed exact path" in item.description
    assert item.signature.startswith("tools." + omitted.name + "(args:")


def test_compact_snapshot_preserves_edit_overloads_and_full_discovery() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})))
    full = build_turn_snapshot(session)
    catalog = snapshot_catalog(full)
    compact = refresh_snapshot_contract(full, compact_catalog=True)
    declarations = next(spec.declarations for spec in compact.model_specs if spec.name == "codemode")
    assert 'edit(args: { "files":' in declarations and "type PatchFileResult" in declarations
    assert 'edit(args: { "path":' not in declarations and "type JsonEditValue" not in declarations
    assert "function describe" in declarations and "read_file(args:" not in declarations
    restored = restore_snapshot(snapshot_description(compact))
    assert _text_request_prompt("", restored) == _text_request_prompt("", compact)
    assert snapshot_catalog(restored) == catalog
    edit = catalog.describe("tools.edit")
    assert edit is not None and "changes: Record" in edit.signature and '"files":' in edit.signature
    minimal = render_declarations(catalog, detail_budget_bytes=0, outer_specs=full.model_specs,
                                  detail_names=("edit",))
    assert "PatchFileResult" not in minimal and "JsonEditValue" not in minimal


def test_session_start_returns_handle_and_wait_reports_events() -> None:
    release = threading.Event()

    def invoke(_name: str, _args: dict[str, object]) -> object:
        release.wait(2)
        return {"ok": True, "value": "receipt"}

    session = CodeModeSession()
    started = time.monotonic()
    handle = session.start(CodeModeRequest(
        "return await tools.read_file({});", (ToolCatalogEntry("read_file", "read"),), invoke,
    ))
    assert time.monotonic() - started < 0.5
    events = list(session.wait(handle, yield_ms=1000))
    assert events[0].kind == "started"
    release.set()
    for _ in range(20):
        events.extend(session.wait(handle, yield_ms=100))
        if any(event.kind == "done" for event in events):
            break
    assert any(event.kind == "child_started" for event in events)
    assert any(event.kind == "child_finished" for event in events)
    assert any(event.kind == "done" for event in events)
    assert [event.seq for event in events] == list(range(len(events)))
    outcome = session.outcome(handle)
    assert outcome.result is not None and outcome.result.ok


def _finish(session: CodeModeSession, handle: CodeModeHandle) -> CodeModeResult:
    for _ in range(50):
        session.wait(handle, yield_ms=100)
        result = session.outcome(handle).result
        if result is not None:
            return result
    pytest.fail("session did not finish")


def test_termination_waits_for_admitted_effect_and_closes_new_calls() -> None:
    admitted, release, settled = threading.Event(), threading.Event(), threading.Event()
    invoked: list[str] = []
    delivery: list[str] = []

    def invoke(name: str, _args: dict[str, object]) -> object:
        invoked.append(name)
        admitted.set()
        assert release.wait(3)
        cancellation.check()
        settled.set()
        return "original receipt"

    def record(call_id: str, _child_index: int | None) -> None:
        assert settled.is_set()
        delivery.append(call_id)

    session = CodeModeSession()
    handle = session.start(CodeModeRequest(
        "await tools.read_file({}); return await tools.read_file({});",
        (ToolCatalogEntry("read_file", "read"),), invoke, on_delivery=record,
    ))
    try:
        assert admitted.wait(3)
        session.wait(handle, yield_ms=1)
        assert session.wait(handle, yield_ms=10)[0].kind == "pending"
        outcome = session.terminate(handle, reason="user stopped")
        assert outcome.result is None and not settled.is_set()
        assert not any(event.kind == "terminated" for event in outcome.events)
    finally:
        release.set()
    result = _finish(session, handle)
    assert result.error_code == "terminated" and settled.is_set()
    assert invoked == ["read_file"] and delivery == ["call-1"]
    assert session.outcome(handle).events[-1].kind == "terminated"


def test_termination_interrupts_a_guest_cpu_loop() -> None:
    delivered = threading.Event()

    def invoke(_name: str, _args: dict[str, object]) -> object:
        return "ready"

    def record(_call_id: str, _child_index: int | None) -> None:
        delivered.set()

    session = CodeModeSession()
    handle = session.start(CodeModeRequest("await tools.read_file({}); while (true) {}",
                                           (ToolCatalogEntry("read_file", "ready"),), invoke, on_delivery=record,
                                           limits=CodeModeLimits(timeout_ms=10_000)))
    assert delivered.wait(3)
    session.wait(handle, yield_ms=1)
    assert session.wait(handle, yield_ms=30)[0].kind == "pending"
    session.terminate(handle, reason="stop CPU loop")
    assert _finish(session, handle).error_code == "terminated"


def test_termination_rejects_queued_tools_before_host_admission() -> None:
    admitted, release = threading.Event(), threading.Event()
    invoked: list[object] = []

    def invoke(_name: str, args: dict[str, object]) -> object:
        invoked.append(args["path"])
        admitted.set()
        assert release.wait(3)
        return "settled"

    session = CodeModeSession()
    handle = session.start(CodeModeRequest(
        "return await Promise.all(['a','b'].map(path=>tools.read_file({path})));",
        (ToolCatalogEntry("read_file", "read"),), invoke,
        limits=CodeModeLimits(max_concurrent_calls=1),
    ))
    try:
        assert admitted.wait(3)
        assert session.terminate(handle, reason="stop queued call").result is None
    finally:
        release.set()
    assert _finish(session, handle).error_code == "terminated"
    assert invoked == ["a"]
    events = session.outcome(handle).events
    assert sum(event.kind == "child_started" for event in events) == 1
    assert sum(event.kind == "child_finished" for event in events) == 1


def test_request_uses_the_original_durable_execution_identity() -> None:
    def invoke(_name: str, _args: dict[str, object]) -> object:
        pytest.fail("unexpected tool call")

    request = CodeModeRequest("return 1;", (), invoke,
                              replay_context={"executionId": "original-kernel-id", "epochMs": 1, "seed": 1})
    assert request.execution_id == "original-kernel-id"


def test_tool_deadline_is_independent_of_observation_budget() -> None:
    def invoke(_name: str, _args: dict[str, object]) -> object:
        while True:
            cancellation.check()
            threading.Event().wait(0.005)

    session = CodeModeSession()
    handle = session.start(CodeModeRequest(
        "return await tools.read_file({});", (ToolCatalogEntry("read_file", "read"),), invoke,
        limits=CodeModeLimits(timeout_ms=3_000), tool_timeout_ms=30,
    ))
    result = _finish(session, handle)
    assert not result.ok and result.error_code == "tool_error" and "timed out" in result.error


def test_recovery_uses_owner_reconstruction_and_rejects_changed_identity() -> None:
    restored: list[str] = []

    def invoke(_name: str, _args: dict[str, object]) -> object:
        pytest.fail("unexpected tool call")

    def restore(execution_id: str) -> CodeModeRequest | CodeModeOutcome:
        restored.append(execution_id)
        if execution_id == "finished":
            return CodeModeOutcome(CodeModeResult(True, value="settled"))
        return CodeModeRequest("return inputs.value;", (), invoke, {"value": "replayed"},
                               execution_id="pending" if execution_id == "pending" else "changed")

    session = CodeModeSession(restore=restore)
    handle = session.recover("pending")
    assert isinstance(handle, CodeModeHandle)
    assert _finish(session, handle).value == "replayed"
    assert isinstance(session.recover("pending"), CodeModeOutcome)
    assert session.recover("finished") == CodeModeOutcome(CodeModeResult(True, value="settled"))
    with pytest.raises(ValueError, match="identity"):
        session.recover("mismatch")
    with pytest.raises(KeyError):
        CodeModeSession().recover("unknown")
    assert restored == ["pending", "finished", "mismatch"]
