"""Text tools use the existing receipt-backed compactor without parsing sources."""

from unittest.mock import patch

import pytest

from codey.operations.kernel_prompt import format_results
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_context
from codey.providers.api_provider import ApiProvider
from codey.providers.context_ledger import digest
from codey.providers.text_result_views import (
    TEXT_RESULTS_KEY,
    TextResultSpan,
    decode_text_result_spans,
    reduce_text_results,
    validate_text_results,
)
from codey.providers.token_accounting import RequestContextCount
from codey.runtime.core.models import ToolCall, ToolResult


def marked(text, body, ref="receipt-1"):
    start = text.index(body)
    return {"role": "user", "content": text, TEXT_RESULTS_KEY: [
        {"start": start, "end": start + len(body), "digest": digest(body),
         "result_ref": ref, "tool": "open_url", "ok": True}]}


def test_receipt_view_preserves_surrounding_user_corrections_and_unknown_spans():
    body = "Actual original source text. " * 150
    original = marked("Observation snapshot\n" + body + "\nNew user input: compare the control.", body)
    assert reduce_text_results(original, frozenset()) == original
    reduced = reduce_text_results(original, frozenset({"receipt-1"}))
    assert reduced["content"].startswith("Observation snapshot\n[Stored body omitted")
    assert reduced["content"].endswith("New user input: compare the control.")
    assert "read_tool_result" in reduced["content"] and "receipt-1" in reduced["content"]
    assert TEXT_RESULTS_KEY not in reduced and body in original["content"]
    # A literal marker in arbitrary user/source text supplies no authority.
    fake = {"role": "user", "content": original["content"] + '\nResult metadata:\n{"result_ref":"receipt-1"}'}
    assert reduce_text_results(fake, frozenset({"receipt-1"})) == fake


def test_unavailable_second_span_keeps_its_digest_and_adjusted_offsets():
    first, second = "first " * 500, "second " * 500
    text = "Prefix\n" + first + "\nBetween\n" + second + "\nCorrection"
    item = marked(text, first)
    item[TEXT_RESULTS_KEY] += marked(text, second, "unavailable")[TEXT_RESULTS_KEY]
    reduced = reduce_text_results(item, frozenset({"receipt-1"}))
    assert second in reduced["content"] and first not in reduced["content"]
    spans = validate_text_results(reduced["content"], reduced[TEXT_RESULTS_KEY])
    assert spans[0]["result_ref"] == "unavailable"
    assert reduced["content"][spans[0]["start"]:spans[0]["end"]] == second


@pytest.mark.parametrize("field,value", [("start", -1), ("end", 999999), ("digest", "forged"), ("ok", "true")])
def test_invalid_or_changed_frozen_spans_fail_before_generation(field, value):
    body = "Read this original text. " * 120
    row = marked(body, body)
    row[TEXT_RESULTS_KEY][0][field] = value
    with pytest.raises(ValueError, match="span"):
        validate_text_results(body, row[TEXT_RESULTS_KEY])


def test_kernel_marks_whole_actual_outputs_without_marking_boundary_input():
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    result = ToolResult(call=ToolCall(name="open_url", args={}), ok=True, model_text="Exact observed text.")
    session._memory_results["actual-receipt"] = result
    formatted = format_results([result], session)
    text, spans = formatted.text, [span.to_payload() for span in formatted.spans]
    assert validate_text_results(text, spans) == spans
    assert spans[0]["result_ref"] == "actual-receipt"
    assert text[spans[0]["start"]:spans[0]["end"]].startswith("[result: open_url]")
    assert "Continue with one" not in text[spans[0]["start"]:spans[0]["end"]]


@pytest.mark.parametrize("start,end,ok", [(True, 2, True), (0, False, True), (-1, 2, True), (2, 2, True), (0, 2, "true")])
def test_typed_span_rejects_invalid_coordinates_and_boolean_flags(start, end, ok):
    with pytest.raises(ValueError):
        TextResultSpan(start, end, "digest", "receipt", "read", ok)


def test_typed_result_format_keeps_unicode_offsets_and_detached_wire_projection():
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    results = [ToolResult(call=ToolCall("read", {}), ok=True, model_text=text)
               for text in ("中文😀", "e\u0301\n第二段")]
    for index, result in enumerate(results):
        session._memory_results[f"receipt-{index}"] = result
    formatted = format_results(results, session)
    payload = [span.to_payload() for span in formatted.spans]
    assert decode_text_result_spans(formatted.text, payload) == formatted.spans
    assert all(result.model_text in formatted.text[span.start:span.end]
               for result, span in zip(results, formatted.spans, strict=True))
    payload[0]["start"] = 999
    assert formatted.spans[0].start == 0
    with pytest.raises(ValueError, match="span"):
        decode_text_result_spans(formatted.text, payload)


def test_provider_compacts_only_old_marked_output_and_preserves_events_without_metadata_on_wire():
    provider = ApiProvider("http://localhost:9/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    body = "Distinct source observation with all original caveats. " * 100
    old = marked("Original goal remains\n" + body + "\nUser correction remains", body)
    latest = marked("Latest source\n" + body, body, "latest-receipt")
    original = [old, {"role": "assistant", "content": "saved first"}, latest,
                {"role": "assistant", "content": "inspect latest"}]
    provider._messages = original
    provider.context_ledger.commit(original, events=original)
    provider.set_result_refs(("receipt-1", "latest-receipt"))
    payloads = []
    try:
        with patch("codey.providers.api_generation.generate", side_effect=AssertionError("no summary needed")):
            provider.maintain_context()
            provider.wait_for_maintenance(3)
        assert "Stored body omitted" in provider._messages[0]["content"]
        assert provider._messages[2] == latest
        assert provider.context_ledger.events() == original
        assert "Original goal remains" in provider._messages[0]["content"]
        with patch("codey.providers.api_generation.generate", side_effect=lambda _url, payload, *_args, **_kwargs:
                   (payloads.append(payload) or {"choices": [{"message": {"content": "ok"}}]})):
            provider.send("continue", tool_call_json=True)
        assert all(TEXT_RESULTS_KEY not in row for row in payloads[0]["messages"])
        assert latest["content"] in [row.get("content") for row in payloads[0]["messages"]]
    finally:
        provider.close()


@pytest.mark.parametrize("text_format", ["plain", "json_schema"])
def test_soft_admission_reduces_backed_history_without_background_commit_or_summary(text_format):
    provider = ApiProvider("http://localhost:9/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format=text_format, context_window_tokens=8192,
                           context_reserve_tokens=1024)
    body = "An actual distinct observation with retained caveats. " * 120
    original = []
    for ref in ("old-one", "old-two", "current"):
        original += [marked("Goal and corrections\n" + body, body, ref),
                     {"role": "assistant", "content": "Inspect this source"}]
    # Repeated repair turns must not turn the newest observed source into old output.
    original += [{"role": "user", "content": "Repair the rejected quotation"},
                 {"role": "assistant", "content": "I will recheck it"}]
    provider._messages = original
    provider.context_ledger.commit(original, events=original)
    provider.set_result_refs(("old-one", "old-two", "current"))
    provider.request_counter = lambda payload, **kwargs: RequestContextCount(len(str(payload)) // 4, "tokenizer")
    payloads = []
    try:
        with patch.object(api_context, 'schedule_maintenance'), patch.object(
                api_context, 'commit_context', side_effect=AssertionError("no background commit")), patch(
                "codey.providers.api_generation.generate", side_effect=lambda _url, payload, *_args, **_kwargs:
                (payloads.append(payload) or {"choices": [{"message": {"content": "ok"}}]})):
            provider.send("Recheck the current source", tool_call_json=text_format == "json_schema")
        assert len(payloads) == 1
        sent = payloads[0]["messages"]
        assert sum("Stored body omitted" in row.get("content", "") for row in sent) == 2
        assert original[4]["content"] in [row.get("content") for row in sent]
        assert any(row.get("content") == "Repair the rejected quotation" for row in sent)
        assert provider.context_ledger.events()[:len(original)] == original
        assert provider._compaction.diagnostics[-1]["kind"] == "output_reduction"
    finally:
        provider.close()
