"""Upgrade recovery reads settled copy receipts without executing the old contract."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.browser_copy import CopyTurn
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelRunRequest,
    KernelTransportDeps,
    result_from_payload,
)
from codey.operations.kernel_driver import drive_kernel, drive_provider_request
from codey.operations.kernel_protocol import ModelInputBinding, TurnSnapshot, refresh_snapshot_contract
from codey.operations.provider_receipts import (
    REQUEST_KIND,
    _restore_spec,
    read_provider_artifact,
    request_artifact_for_effect,
    restore_snapshot,
    snapshot_description,
)
from codey.operations.provider_request import prepare_provider_request
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.requests import ProviderRequest, SendTextRequest
from codey.providers.transport import ProviderTransport
from codey.research.browser_search import BrowserSearchProvider
from codey.runtime.core.operation_payload import LEAF_KERNEL_SETTLED
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_model_input_binding import URL, copy_snapshot
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target

SOURCE: JsonObject = {"key_column": "Batch", "key": "RZ-824", "column": "Balance"}
ARGS: JsonObject = {"source": SOURCE, "target": {"file": "vouchers.json", "field": "outstanding"}}


def legacy_spec() -> ToolSpec:
    fixture = parse_json_object((Path(__file__).parent / "fixtures" / "browser_copy_legacy_binding.json").read_text(encoding="utf-8"))
    # Keep the obsolete model input intact while using the current ToolSpec
    # container. This copy operation never supported background execution.
    spec = decode_json_object(decode_json_object(fixture["input_binding"])["model_spec"])
    return _restore_spec({**spec, "supports_background": False})


def legacy_snapshot() -> TurnSnapshot:
    return refresh_snapshot_contract(replace(copy_snapshot(table=True),
        input_binding=ModelInputBinding("browser_copy", legacy_spec())))


class RecordedProvider(ProviderTransport):
    provider: object = None

    def __init__(self, reply: AssistantTurn | str = "") -> None:
        self.reply = reply
        self.requests = 0

    def execute_request(self, request: ProviderRequest) -> AssistantTurn | str:
        self.requests += 1
        assert self.reply, "recovery must not request another model response"
        return self.reply


class InterruptedAtResult(BaseException):
    """Stop after the durable result commits, before the outer task settles."""


def test_legacy_snapshot_keeps_frozen_definition_but_cannot_execute() -> None:
    original = legacy_snapshot()
    restored = restore_snapshot(snapshot_description(original))
    assert restored.input_binding == original.input_binding
    assert restored.contract_text == original.contract_text
    assert not restored.implementation_compatible


@pytest.mark.parametrize("native", [False, True])
def test_pending_legacy_call_is_rejected_before_tool_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool) -> None:
    snapshot = legacy_snapshot()
    assert snapshot.policy is not None
    durable = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite"))
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="copy", provider_snapshot={})
    durable.claim_task("r")
    durable.start_task("r", state=dict(durable.task("r").state))
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4, max_repair_rounds=1,
                               task_kind="hybrid")
    mutations.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
                            managed_outputs=ManagedOutputStore(tmp_path))
    action: JsonObject = {"tool": "codemode", "args": ARGS}
    provider = RecordedProvider(AssistantTurn(tool_calls=(ProviderToolCall("copy", "codemode", ARGS),)) if native else
                                json.dumps(action))
    request = KernelRunRequest(transport=KernelTransportDeps(provider=provider, run_id="r", provider_id="local"),
                               execution=KernelExecutionDeps(intent_sink=sink, project_path=tmp_path))
    session = TaskSession(snapshot.policy, task_kind="hybrid", project=str(tmp_path))

    def old_spec(_copy: CopyTurn, _original: ToolSpec) -> ToolSpec:
        return legacy_spec()

    try:
        sink.bind_request(snapshot, logical_turn=1)
        reference = prepare_provider_request(sink, SendTextRequest("copy"), counters=None)
        with monkeypatch.context() as old_runtime:
            old_runtime.setattr(CopyTurn, "spec", old_spec)
            drive_provider_request(sink, provider, reference)
        result = drive_kernel(session, request)
        assert result.stop_reason == "recovery_failure" and "incompatible" in result.summary
        assert provider.requests == 1
        assert not session._memory_results
        assert all(effect.category != "tool_call" for effect in durable.effects("r"))
    finally:
        durable.close()


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_completed_legacy_copy_returns_stored_result_after_restart_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    file = project / "vouchers.json"
    file.write_text('{"outstanding":0,"keep":"original"}\n', encoding="utf-8")
    replies: list[JsonObject] = [{"tool": "codemode", "args": {}}, {"tool": "codemode", "args": ARGS},
                                {"tool": "done", "args": {"summary": "Copied balance 962."}}]
    install_http(monkeypatch, replies)

    def route(_provider: BrowserSearchProvider, request: Route) -> None:
        request.fulfill(status=200, content_type="text/html", body=(
            "<table><thead><tr><th>Batch</th><th>Balance</th></tr></thead>"
            "<tbody><tr><td>RZ-824</td><td>962</td></tr></tbody></table>"))

    monkeypatch.setattr(BrowserSearchProvider, "_guard_request", route)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", dict)

    def unavailable_review(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("codey.app.review_service.run_review", unavailable_review)
    target = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)
    recording = ExperimentProvider(target, tmp_path, fixed_acquisition=False)

    def connect(*_args: object, **_kwargs: object) -> ExperimentProvider:
        return recording

    original_spec = CopyTurn.spec
    original_restore = KernelContext.restore_facts
    stopped: list[KernelContext] = []

    def old_spec(copy: CopyTurn, original: ToolSpec) -> ToolSpec:
        return legacy_spec() if copy.phase == "edit" else original_spec(copy, original)

    def stop_before_result(context: KernelContext) -> None:
        current = context.load_view().state
        if current is not None and current.leaf == LEAF_KERNEL_SETTLED:
            stopped.append(context)
            raise InterruptedAtResult
        original_restore(context)

    selection: dict[str, object] = {"model": target.model, "base_url": target.base_url}
    events: list[dict[str, object]] = []
    try:
        with monkeypatch.context() as old_runtime:
            old_runtime.setattr(CopyTurn, "spec", old_spec)
            old_runtime.setattr(KernelContext, "restore_facts", stop_before_result)
            with pytest.raises(InterruptedAtResult):
                run_headless(HeadlessRequest(project,
                    f"把 {URL} 中 RZ-824 的 Balance 原样写入 vouchers.json 的 outstanding，整数。保留其他字段。",
                    intent="hybrid", provider_id="local", max_turns=3, state_home=state,
                    research_store_root=tmp_path / "knowledge", model_selection=selection,
                    requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect")),
                    emit_jsonl=events.append, connect_provider=connect)
    finally:
        recording.close()
    previous, = stopped
    run_id, session_id = previous.sink.run_id, previous.sink.session_id
    assert parse_json_object(file.read_text(encoding="utf-8")) == {"outstanding": 962, "keep": "original"}
    initial_receipts = tool_receipts(state)
    edits = [row for row in initial_receipts if row.result.call.name == "edit"]
    assert len(edits) == 1 and edits[0].result.ok
    # An already completed result must not run its successful write again.
    later = b'{"outstanding":962,"keep":"original","external":"after completion"}\n'
    file.write_bytes(later)
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    provider = RecordedProvider()
    sink = KernelEffectSink(RuntimeMutationLine(durable=durable), session_id=session_id, run_id=run_id,
        provider_id="local", durable=durable, managed_outputs=ManagedOutputStore(state))
    effects = RuntimeEffectStore(durable=durable).load_effects(session_id, run_id)
    response = next(effect for effect in reversed(effects) if effect.intent.effect_category == "provider_send")
    frozen = read_provider_artifact(sink.managed_outputs, request_artifact_for_effect(durable, response),
        kind=REQUEST_KIND, session_id=session_id, run_id=run_id, effect_id=response.intent.effect_id)
    policy = TaskPolicy.from_payload(decode_json_object(frozen["snapshot"])["policy"])
    scope = require_json_string(frozen["effect_scope"], "effect scope")
    request = KernelRunRequest(transport=KernelTransportDeps(provider=provider, provider_id="local", run_id=run_id,
        effect_scope=scope), execution=KernelExecutionDeps(intent_sink=sink, project_path=project))
    session = TaskSession(policy, task_kind="hybrid", project=str(project))
    try:
        context = KernelContext(session, request)
        current = context.load_view().state
        assert current is not None
        decision = context.decision(current)
        assert decision["purpose"] != "retry"
        expected = result_from_payload(decision["result"])
        assert expected.completed and expected.stop_reason == "done"
        before = durable.task_facts(run_id)
        recovered = drive_kernel(session, request)
        assert recovered == expected, recovered.summary
        assert drive_kernel(session, request) == expected
        assert session.last_done_text == "Copied balance 962."
        assert durable.task_facts(run_id) == before
        assert provider.requests == 0
        assert file.read_bytes() == later
        assert any(receipt.call.name == "edit" and receipt.ok for receipt in session._memory_results.values())
    finally:
        durable.close()
    assert tool_receipts(state) == initial_receipts
