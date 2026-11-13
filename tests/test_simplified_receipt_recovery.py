"""Recovery facts, frozen contracts and unavailable rereads share production paths."""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from codey.codemode.kernel import _simplified_failure
from codey.operations.browser_copy import CAPTURE, CopyTarget, copy_turn
from codey.operations.kernel_prompt import _result_context
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_guidance import task_result_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_context import bind_result_recovery
from codey.providers.api_provider import ApiProvider
from codey.providers.base import render_result_metadata
from codey.providers.context_checkpoint import reduce_old_outputs, select_range
from codey.providers.context_ledger import digest
from codey.providers.error_classification import ContextOverflowError
from codey.providers.text_result_views import TEXT_RESULTS_KEY, TextResultSpan
from codey.providers.token_accounting import RequestContextCount
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE, STALE_READ_REFERENCE_ERROR
from codey.toolchain.tool_prompt import contract_semantics
from codey.types.json_value import JsonObject, parse_json_object
from tests.test_model_input_binding import copy_snapshot, upload_snapshot


def observed_receipts() -> dict[str, ToolResult]:
    data: JsonObject = {"tables": [], "records": [{"id": "json-1", "text": '{"label":"00017"}'}]}
    return {
        "capture": ToolResult(ToolCall("browser_eval", {"code": CAPTURE}, call_id="original:child:0"), "observed",
            ok=True, canonical={"value": {"value": data, "truncated": False}}),
        "parent": ToolResult(ToolCall("codemode", {}), "missing return", ok=False,
            canonical={"child_effect_ids": ["capture"]}, audit={"codemode_execution_id": "original"}),
    }


def test_failed_parent_reuses_owned_complete_observation_and_delivers_its_body() -> None:
    receipts = observed_receipts()
    target = CopyTarget("https://example.com/", "dispatch.json", "label", "string")
    turn = copy_turn(target, "copy", receipts)
    assert turn.phase == "edit" and turn.ref == "capture"
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    session._memory_results.update(receipts)
    text = _result_context(receipts["parent"], session, receipt_recovery=False)
    assert "00017" in text and "json-1" in text and "read_tool_result" not in text


@pytest.mark.parametrize("case", ["foreign", "missing", "duplicate", "gap", "truncated", "unlisted"])
def test_invalid_parent_child_observation_cannot_trigger_a_source_refetch(case: str) -> None:
    receipts = observed_receipts()
    parent, child = receipts["parent"], receipts["capture"]
    if case == "foreign":
        receipts["parent"] = replace(parent, audit={"codemode_execution_id": "other"})
    elif case == "missing":
        receipts["parent"] = replace(parent, canonical={"child_effect_ids": ["capture", "absent"]})
    elif case == "duplicate":
        receipts["parent"] = replace(parent, canonical={"child_effect_ids": ["capture", "capture"]})
    elif case == "gap":
        receipts["capture"] = replace(child, call=replace(child.call, call_id="original:child:1"))
    elif case == "unlisted":
        receipts["unlisted"] = ToolResult(ToolCall("edit", {}, call_id="original:child:1"), "written", ok=True)
    else:
        receipts["capture"] = replace(child, truncated=True)
    with pytest.raises(ValueError, match="must not be fetched again"):
        copy_turn(CopyTarget("https://example.com/", "dispatch.json", "label", "string"), "copy", receipts)


@pytest.mark.parametrize("state", ["written", "conflict", "uncertain"])
def test_simplified_failure_guidance_matches_write_facts(state: str) -> None:
    binding = copy_snapshot().input_binding
    assert binding is not None
    child = ToolResult(ToolCall("edit", {}), STALE_READ_REFERENCE_ERROR if state == "conflict" else "write outcome",
                       ok=state == "written", audit={"error_code": STALE_READ_REFERENCE_CODE} if state == "conflict" else {})
    result = _simplified_failure(ToolResult(ToolCall("codemode", {}), "failed", ok=False), binding.model_spec, {"edit": child})
    if state == "written":
        assert "file write completed" in result.model_text and "Do not repeat the write" in result.model_text
    elif state == "conflict":
        assert "Retry with the same source" in result.model_text and "Do not replay the whole operation" not in result.model_text
    else:
        assert "delivery is uncertain" in result.model_text and "Retry with the same source;" not in result.model_text


@pytest.mark.parametrize("kind", ["upload", "copy"])
def test_restored_text_contract_matches_model_semantics_with_sorted_json(kind: str) -> None:
    snapshot = upload_snapshot() if kind == "upload" else copy_snapshot()
    payload = parse_json_object(json.dumps(snapshot_description(snapshot), sort_keys=True))
    assert restore_snapshot(payload) == snapshot
    payload["contract_text"] = "unrelated instructions"
    with pytest.raises(ValueError, match="contract text"):
        restore_snapshot(payload)


def test_signature_semantics_keep_field_type_and_optional_associations() -> None:
    original = '  copy(args: { "source": { "a": string; "b"?: number }; "target": boolean })'
    reordered = '  copy(args: { "target": boolean; "source": { "b"?: number; "a": string } })'
    assert contract_semantics(original) == contract_semantics(reordered)
    assert contract_semantics(original) != contract_semantics(reordered.replace('"a": string', '"a"?: string'))
    assert contract_semantics(original) != contract_semantics(reordered.replace('"b"?: number', '"b"?: string'))


@pytest.mark.parametrize("tool", ["codemode", "read_tool_result", "open_url"])
def test_result_guidance_only_offers_currently_visible_rereads(tool: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    result = ToolResult(ToolCall(tool, {}), "full retained body", ok=True,
        canonical={"receipt_page": {"result_ref": "original", "view": "output", "next_offset": 10}})
    session._memory_results["current"] = result
    hidden = task_result_context(session, result, None, tool_names=("codemode", "ask_user"))
    assert "use read_tool_result" not in hidden and "read_tool_result(" not in hidden and "short handle" not in hidden
    assert "read_tool_result" in task_result_context(session, result, None, tool_names=("read_tool_result",))


def marked_history(native: bool) -> tuple[list[dict[str, object]], str]:
    body = "\n".join(f"record {i}: unique value {i * 7919}" for i in range(300))
    body += "\n" + render_result_metadata("original", tool="open_url", ok=True)
    item: dict[str, object] = {"role": "user", "content": body, TEXT_RESULTS_KEY: [
        TextResultSpan(0, len(body), digest(body), "original", "open_url", True).to_payload()]}
    if native:
        items: list[dict[str, object]] = [{"role": "user", "content": "goal"},
            {"role": "assistant", "tool_calls": [{"id": "call", "type": "function", "function": {"name": "open_url", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call", "content": body}]
    else:
        items = [{"role": "user", "content": "goal"}, item, {"role": "assistant", "content": "observed"}]
    items += [{"role": "user", "content": "continue"}, {"role": "assistant", "content": "next"},
              {"role": "user", "content": "latest"}, {"role": "assistant", "content": "ready"}]
    if native:
        items += [{"role": "assistant", "tool_calls": [{"id": "new", "type": "function", "function": {"name": "open_url", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "new", "content": "current result"}]
    else:
        current = "current result"
        items += [{"role": "user", "content": current, TEXT_RESULTS_KEY: [
            TextResultSpan(0, len(current), digest(current), "new", "open_url", True).to_payload()]},
            {"role": "assistant", "content": "current observed"}]
    items += [{"role": "user", "content": "now"}, {"role": "assistant", "content": "ready"}]
    return items, body


@pytest.mark.parametrize("native", [False, True])
def test_unavailable_reread_preserves_body_in_both_reduction_and_semantic_selection(native: bool) -> None:
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    items, body = marked_history(native)
    try:
        retained = reduce_old_outputs(provider._codec, items, frozenset())
        assert retained == items if not native else "Stored body omitted" not in str(retained)
        selected = select_range(provider._codec, items, result_refs=frozenset())
        if selected is not None:
            assert "record 299:" not in str(items[selected.start:selected.end])
        assert "record 299:" not in str(reduce_old_outputs(provider._codec, items, frozenset({"original"})))
    finally:
        provider.close()


def test_over_budget_unrecoverable_body_fails_before_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=2000)
    items, body = marked_history(False)
    provider._messages = items
    def count(payload: dict[str, object], *, deadline: float) -> RequestContextCount:
        return RequestContextCount(len(str(payload)), "tokenizer")

    def summarize(source: list[dict[str, object]], deadline: float) -> str:
        assert "record 299:" not in str(source)
        return "Narrative history only."

    provider.request_counter = count
    monkeypatch.setattr(provider, "summarize_context", summarize)
    try:
        with patch("codey.providers.api_generation.generate", side_effect=AssertionError("must not send")), pytest.raises(ContextOverflowError):
            provider.send("continue", timeout=5)
        assert "record 299:" in str(provider._messages)
    finally:
        provider.close()


@pytest.mark.parametrize("native", [False, True])
def test_hiding_reread_restores_previously_omitted_bodies_from_the_existing_archive(native: bool) -> None:
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    items, body = marked_history(native)
    reduced = reduce_old_outputs(provider._codec, items, frozenset({"original"}))
    assert "record 299:" not in str(reduced)
    checkpoint: dict[str, object] = {"kind": "output_reduction", "source": items,
                                    "view_digests": [digest(item) for item in reduced]}
    provider._messages = reduced
    provider.context_ledger.commit(reduced, checkpoint=checkpoint)
    try:
        bind_result_recovery(provider, ())
        # Text spans and native call identities are restored alongside the body.
        assert provider._messages == items
        assert provider._result_refs == frozenset()
    finally:
        provider.close()


def test_hidden_reread_restores_a_summary_of_an_already_reduced_body() -> None:
    from codey.providers.context_checkpoint import restore_result_bodies

    provider = ApiProvider("http://localhost:9/v1", "fixture")
    items, _ = marked_history(False)
    reduced = reduce_old_outputs(provider._codec, items, frozenset({"original"}))
    checkpoint: dict[str, object] = {"kind": "output_reduction", "source": items,
                                    "view_digests": [digest(item) for item in reduced]}
    summary: dict[str, object] = {"role": "assistant", "content": "A summary without the original value."}
    semantic: dict[str, object] = {"kind": "work_state", "item_digest": digest(summary), "source": reduced}
    try:
        assert restore_result_bodies([summary], [checkpoint, semantic], frozenset()) == items
        assert restore_result_bodies([summary], [checkpoint, semantic], frozenset({"original", "new"})) == [summary]
    finally:
        provider.close()


def test_background_view_cannot_commit_after_reread_visibility_changes() -> None:
    from codey.providers.api_context import commit_context, context_snapshot
    from codey.providers.context_checkpoint import ContextRange

    provider = ApiProvider("http://localhost:9/v1", "fixture")
    items: list[dict[str, object]] = [{"role": "assistant", "content": "historical narrative " * 500}]
    provider._messages = items
    provider.set_result_refs(("original",))
    snapshot = context_snapshot(provider)
    selected = ContextRange(0, 1, digest(items))

    def count(payload: dict[str, object], *, deadline: float) -> RequestContextCount:
        provider.set_result_refs(())
        return RequestContextCount(10, "tokenizer")

    provider.request_counter = count
    try:
        assert not commit_context(provider, selected, [{"role": "assistant", "content": "summary"}],
                                  {"kind": "work_state", "text": "summary", "source": items}, snapshot, None, 0)
        assert provider._messages == items and provider.context_ledger.revision == 0
    finally:
        provider.close()


def test_initial_text_prompt_uses_the_already_finalized_snapshot() -> None:
    from codey.operations.kernel_preparation import prepare_kernel_turn

    snapshot = copy_snapshot()
    assert snapshot.policy is not None
    session = TaskSession(snapshot.policy, task_kind="hybrid", task_text="copy the observed value")
    prepared = prepare_kernel_turn(session, user_task=session.task_text, context_text="", task_guidance="",
                                   native=False, snapshot=snapshot)
    assert prepared.snapshot is snapshot
    assert "Visible tools: codemode, ask_user" in prepared.prompt
    assert "read_tool_result(" not in prepared.prompt
