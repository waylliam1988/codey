from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode.kernel import _simplified_failure
from codey.operations.kernel_prompt import _result_context
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.providers.api_provider import ApiProvider
from codey.providers.base import render_result_metadata, result_metadata_from_text
from codey.providers.context_checkpoint import reduce_old_outputs, select_range
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE, STALE_READ_REFERENCE_ERROR
from codey.toolchain.runtime import write_file
from tests.test_model_input_binding import copy_snapshot
from tests.test_simplified_receipt_recovery import marked_history, observed_receipts


@pytest.mark.parametrize("marker", ["canonical", "audit"])
@pytest.mark.parametrize("complete_parent", [True, False])
@pytest.mark.parametrize("earlier_success", [True, False])
def test_applied_unconfirmed_copy_blocks_the_next_snapshot(marker: str, complete_parent: bool,
                                                          earlier_success: bool) -> None:
    policy = copy_snapshot().policy
    assert policy is not None
    session = TaskSession(policy, task_kind="hybrid",
        task_text="Copy the value from https://example.com/ into dispatch.json field label as a string.")
    session._memory_results.update(observed_receipts())
    assert build_turn_snapshot(session).browser_copy is not None
    if earlier_success:
        session._memory_results["earlier-write"] = ToolResult(
            ToolCall("edit", {"path": "dispatch.json", "browser_source": {"ref": "capture"}},
                     call_id="earlier:child:0"), "written", ok=True)
        session._memory_results["earlier-parent"] = ToolResult(ToolCall("codemode", {}), "written", ok=True,
            canonical={"child_effect_ids": ["earlier-write"]}, audit={"codemode_execution_id": "earlier"})
    child = ToolResult(ToolCall("edit", {"path": "dispatch.json", "browser_source": {"ref": "capture"}},
                               call_id="write:child:0"), "identity unconfirmed", ok=False,
                       canonical={"mutation_applied": True} if marker == "canonical" else {},
                       audit={"mutation_applied": True} if marker == "audit" else {})
    session._memory_results["write-child"] = child
    if complete_parent:
        session._memory_results["write-parent"] = ToolResult(ToolCall("codemode", {}), "failed", ok=False,
            canonical={"child_effect_ids": ["write-child"]}, audit={"codemode_execution_id": "write"})
    with pytest.raises(ValueError, match="already occurred.*Do not repeat"):
        build_turn_snapshot(session)


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_encoded_native_result_stays_protected_when_reread_is_hidden(protocol: str) -> None:
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol)
    items, _ = marked_history(True)
    metadata = render_result_metadata("original", tool="open_url", ok=True)
    items[2]["content"] = "unique observation preserved\n" + "repeated value\n" * 600 + metadata
    key = "content"
    if protocol == "openai-responses":
        items[1] = {"type": "function_call", "call_id": "call", "name": "open_url", "arguments": "{}"}
        items[2] = {"type": "function_call_output", "call_id": "call", "output": items[2]["content"]}
        items[-4] = {"type": "function_call", "call_id": "new", "name": "open_url", "arguments": "{}"}
        items[-3] = {"type": "function_call_output", "call_id": "new", "output": "current result"}
        key = "output"
    try:
        provider._codec.validate_view(items)
        reduced = reduce_old_outputs(provider._codec, items, frozenset())
        assert "text_encoding" in str(reduced[2][key])
        encoded = reduced[2][key]
        assert isinstance(encoded, str)
        assert result_metadata_from_text(encoded) == result_metadata_from_text(str(items[2][key]))
        selected = select_range(provider._codec, reduced, result_refs=frozenset())
        assert selected is None or not selected.start <= 2 < selected.end
        available = select_range(provider._codec, reduced, result_refs=frozenset({"original", "new"}))
        assert available is not None and available.start <= 2 < available.end
        assert reduce_old_outputs(provider._codec, reduced, frozenset()) == reduced
    finally:
        provider.close()


@pytest.mark.parametrize("content", ["old", "new"])
def test_conflict_producer_has_a_structured_reason(tmp_path: Path, content: str) -> None:
    (tmp_path / "target.json").write_text("old", encoding="utf-8")
    result = write_file(tmp_path, "target.json", content, expected_version="outdated")
    assert not result.ok and result.audit["error_code"] == STALE_READ_REFERENCE_CODE
    assert (tmp_path / "target.json").read_text(encoding="utf-8") == "old"


def test_simplified_conflict_view_keeps_raw_diagnostics_out_of_the_model_context() -> None:
    binding = copy_snapshot().input_binding
    assert binding is not None
    child = ToolResult(ToolCall("edit", {}, call_id="write:child:0"), STALE_READ_REFERENCE_ERROR, ok=False,
                       audit={"error_code": STALE_READ_REFERENCE_CODE})
    parent = ToolResult(ToolCall("codemode", {}), STALE_READ_REFERENCE_ERROR, ok=False,
                        canonical={"child_effect_ids": ["child"]}, audit={"codemode_execution_id": "write"})
    projected = _simplified_failure(parent, binding.model_spec, {"child": child})
    policy = copy_snapshot().policy
    assert policy is not None
    session = TaskSession(policy)
    session._memory_results.update(child=child, parent=projected)
    text = _result_context(projected, session, receipt_recovery=False)
    assert "Retry with the same source" in text
    for instruction in ("read_file", "readResult", "base_result_ref", "repair_of", "tools.edit"):
        assert instruction not in text
    assert session._memory_results["child"] is child and child.model_text == STALE_READ_REFERENCE_ERROR
    assert projected.audit["runtime_error"] == parent.model_text
    assert "readResult" in _result_context(parent, session, receipt_recovery=False)


def test_error_words_alone_do_not_authorize_a_simplified_retry() -> None:
    binding = copy_snapshot().input_binding
    assert binding is not None
    child = ToolResult(ToolCall("edit", {}), STALE_READ_REFERENCE_ERROR, ok=False)
    parent = ToolResult(ToolCall("codemode", {}), "failure", ok=False)
    projected = _simplified_failure(parent, binding.model_spec, {"child": child})
    assert "Retry with the same source;" not in projected.model_text
    applied = replace(child, audit={"mutation_applied": True, "error_code": STALE_READ_REFERENCE_CODE})
    projected = _simplified_failure(parent, binding.model_spec, {"child": applied})
    assert "already occurred" in projected.model_text and "Do not repeat" in projected.model_text
    assert "Retry with the same source;" not in projected.model_text
