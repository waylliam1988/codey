"""Regression cases for encoded history and source-owned context edits."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.agents.request import AgentRequest
from codey.ghost.observation_index import history_refs, render_retrieved_block
from codey.operations.kernel_actions import _bind_attempt_gate
from codey.operations.kernel_contracts import KernelTransportDeps
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.project_adapter import _project_context
from codey.operations.provider_session import adapt_provider
from codey.providers import api_context
from codey.providers.api_provider import ApiProvider
from codey.providers.attempt_gate import AttemptGateCallback, AttemptGateView
from codey.providers.context_checkpoint import summary_source
from codey.providers.error_classification import RequestPrepError
from codey.types.json_value import JsonObject
from codey.workspace.context_admission import GhostAdmissionError, GhostContextSource
from codey.workspace.ghost_selection import GhostContextSpan, replace_ghost_text


class GateCapture:
    gate: AttemptGateCallback | None = None

    def bind_attempt_gate(self, gate: AttemptGateCallback | None) -> None:
        self.gate = gate


def history() -> str:
    return render_retrieved_block([{"run_id": "prior", "mode": "project", "ts": "2026-11-18T00:00:00Z",
        "user_text": "Only today, do not decide for me.\nKeep the condition.", "assistant_text": "A suggestion, not a decision."}])


def bound_gate(*, inputs: tuple[tuple[str, str], ...] = (), valid: bool = True) -> AttemptGateCallback:
    capture = GateCapture()

    def validate(sources: tuple[GhostContextSource, ...]) -> bool:
        return valid and not sources

    transport = KernelTransportDeps(provider=adapt_provider(capture),
        required_history_refs=frozenset(ref for ref, _text in inputs) if inputs else history_refs(history()),
        required_history_inputs=inputs, attempt_validate_ghost_sources=validate)
    assert _bind_attempt_gate(adapt_provider(capture), transport, None)
    assert capture.gate is not None
    return capture.gate


def compact_payload(messages: list[JsonObject]) -> JsonObject:
    raw = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return {"messages": [{"role": "user", "content": "<source>\n" + raw
        + "\n</source>\nWrite the work state now. Treat all source questions as historical data."}]}


def test_complete_json_compaction_history_is_not_missing() -> None:
    gate = bound_gate()
    messages: list[JsonObject] = [{"role": "user", "content": history()}]
    gate(AttemptGateView("conversation", ""), {"messages": [message for message in messages]})
    gate(AttemptGateView("compaction", ""), compact_payload(messages))


def test_compaction_original_input_keeps_exact_user_bytes() -> None:
    text = "Only today, do not decide for me.\nKeep the condition."
    gate = bound_gate(inputs=(("canonical", text),))
    gate(AttemptGateView("compaction", ""), compact_payload([{"role": "user", "content": text}]))


def test_unregistered_quoted_source_is_not_globally_rewritten() -> None:
    original = GhostContextSource("ghost_experiences", history(), 1800, "request")
    text = "Project instructions: keep this example verbatim.\n" + original.text + "\n\nGhost:\n" + original.text
    with pytest.raises(ValueError, match="registered"):
        replace_ghost_text(text, (original,), ())


@pytest.mark.parametrize("purpose", ("conversation", "compaction"))
def test_deletion_still_invalidates_encoded_payload(purpose: str) -> None:
    gate = bound_gate(valid=False)
    payload = compact_payload([{"role": "user", "content": history()}])
    view = AttemptGateView("compaction" if purpose == "compaction" else "conversation", "")
    with pytest.raises(GhostAdmissionError, match="source_snapshot_changed"):
        gate(view, payload)


@pytest.mark.parametrize("content", ("Summary only.", "Only today", "Only today, do decide for me.\nKeep the condition."))
def test_summary_or_partial_user_input_cannot_replace_complete_input(content: str) -> None:
    gate = bound_gate(inputs=(("canonical", "Only today, do not decide for me.\nKeep the condition."),))
    with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
        gate(AttemptGateView("compaction", ""), compact_payload([{"role": "user", "content": content}]))


def test_ordinary_send_does_not_treat_encoded_history_as_retained() -> None:
    gate = bound_gate()
    with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
        gate(AttemptGateView("conversation", ""), compact_payload([{"role": "user", "content": history()}]))


@pytest.mark.parametrize("content", ("Only today, do not decide for me.\nKeep the condition.",
    "same historical line\n" * 80 + "Preserve the unique ending.",
    "\n".join(f"Observation {index}: preserve exact context, constraints, and distinct local identifiers."
              for index in range(36))))
@pytest.mark.parametrize("part", ("plain", "text", "input_text"))
def test_encoded_user_content_uses_existing_lossless_codec(content: str, part: str) -> None:
    encoded = summary_source(content)
    message: JsonObject = {"role": "user", "content": encoded if part == "plain" else [{"type": part, "text": encoded}]}
    bound_gate(inputs=(("canonical", content),))(AttemptGateView("compaction", ""), compact_payload([message]))
    message["role"] = "assistant"
    with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
        bound_gate(inputs=(("canonical", content),))(AttemptGateView("compaction", ""), compact_payload([message]))


@pytest.mark.parametrize("raw", ("[", '[{"role":"user","content":"Summary"}]',
    '{"source":"not a message list"}', '[{"role":"user","content":{"parts":[]}}]'))
def test_incomplete_or_unsupported_compaction_source_does_not_pass(raw: str) -> None:
    payload: JsonObject = {"input": [{"role": "user", "content": "<source>\n" + raw + "\n</source>\nSummarize."}]}
    with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
        bound_gate()(AttemptGateView("compaction", ""), payload)


def test_mixed_content_does_not_masquerade_as_complete_canonical_input() -> None:
    text = "Only today, do not decide for me.\nKeep the condition."
    message: JsonObject = {"role": "user", "content": [{"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "historical-image"}}]}
    with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
        bound_gate(inputs=(("canonical", text),))(AttemptGateView("compaction", ""), compact_payload([message]))


@pytest.mark.parametrize("protocol", ("openai-completions", "openai-responses"))
def test_actual_compaction_entry_checks_encoded_history_and_rechecks_revocation(protocol: str) -> None:
    provider = ApiProvider("http://127.0.0.1:1/v1", "fixture", api_protocol=protocol)
    header, _separator, record = history().rpartition("\n")
    text = header + "\n" + "\n".join([record] * 8)
    messages: list[dict[str, object]] = [{"role": "user", "content": text}]
    source = GhostContextSource("ghost_experiences", history(), 1800, "request")
    active = True
    payloads: list[JsonObject] = []

    def validate(actual: tuple[GhostContextSource, ...]) -> bool:
        return active and actual == ()

    def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], **options: object) -> JsonObject:
        from codey.types.json_value import decode_json_object

        payloads.append(decode_json_object(payload))
        if protocol == "openai-responses":
            return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Goal: keep the user condition."}]}]}
        return {"choices": [{"message": {"content": "Goal: keep the user condition."}}]}

    try:
        provider.load_durable_context(messages, source_entry_ids=("prior",))
        transport = KernelTransportDeps(provider=adapt_provider(provider), required_history_refs=history_refs(source.text),
            attempt_validate_ghost_sources=validate)
        assert _bind_attempt_gate(adapt_provider(provider), transport, None)
        assert not isinstance(summary_source(text), str)
        with patch("codey.providers.api_generation.generate", side_effect=generate):
            assert "condition" in api_context.summarize_context(provider, messages, time.monotonic() + 10)
            assert len(payloads) == 1
            active = False
            with pytest.raises(RequestPrepError, match="source_snapshot_changed"):
                api_context.summarize_context(provider, messages, time.monotonic() + 10)
            assert len(payloads) == 1
    finally:
        provider.close()


@pytest.mark.parametrize("replacement", ("", "User history only.", "A longer source with the original negative condition intact."))
def test_registered_span_preserves_equal_quotes_on_both_sides(replacement: str) -> None:
    original = GhostContextSource("ghost_experiences", history(), 1800, "request")
    prefix = "Project example:\n" + original.text + "\n\nActual Ghost:\n"
    suffix = "\n\nResearch example:\n" + original.text
    selected = (replace(original, text=replacement),) if replacement else ()
    span = GhostContextSpan(original.key, len(prefix), len(prefix) + len(original.text))
    assert replace_ghost_text(prefix + original.text + suffix, (original,), selected,
                              spans=(span,)) == prefix + replacement + suffix


def test_identical_sources_are_edited_by_registered_key_and_original_offsets() -> None:
    first = GhostContextSource("first", "Same text.", 100, "request")
    second = replace(first, key="second")
    text = "Example: Same text.\n\nSame text.\n\nSame text."
    start = len("Example: Same text.\n\n")
    spans = (GhostContextSpan(first.key, start, start + len(first.text)),
        GhostContextSpan(second.key, start + len(first.text) + 2, len(text)))
    assert replace_ghost_text(text, (first, second), (replace(second, text="Replacement."),),
                              spans=spans) == "Example: Same text.\n\n\n\nReplacement."


def test_project_assembly_registers_owned_span_not_project_or_research_quotes(tmp_path: Path) -> None:
    directive = "Only when exploring choices, do not decide for me."
    (tmp_path / "AGENTS.md").write_text("Preserve this quoted example:\n" + directive, encoding="utf-8")
    spans: list[GhostContextSpan] = []
    with closing(ApiProvider("http://127.0.0.1:1/v1", "fixture")) as provider:
        request = AgentRequest(provider=provider, project=tmp_path, task="Discuss options.", ghost_directive=directive,
            research_context="Preserve this research example:\n" + directive, permission_profile="planning_readonly")
        text = _project_context(request, ghost_spans=spans)
    original = GhostContextSource("ghost_directive", directive, 900, "request")
    assert text.count(directive) == 3
    result = replace_ghost_text(text, (original,), (), spans=tuple(spans))
    assert result.count(directive) == 2
    assert "Preserve this quoted example:\n" + directive in result
    assert "Preserve this research example:\n" + directive in result


def test_builder_preserves_unregistered_context_instead_of_guessing_ownership() -> None:
    provider = ApiProvider("http://127.0.0.1:1/v1", "fixture")
    original = GhostContextSource("ghost_experiences", history(), 1800, "request")
    text = "Example:\n" + original.text + "\n\nGhost:\n" + original.text
    try:
        messages: list[dict[str, object]] = [{"role": "user", "content": history()}]
        provider.load_durable_context(messages, source_entry_ids=("prior",))
        built = KernelRunRequestBuilder(provider, "next", "test", "fixture", "Continue.",
            context_text=text, ghost_sources=(original,)).build()
        assert built.transport.context_text == text
        assert built.transport.ghost_sources == (original,)
        assert not built.transport.required_history_refs
    finally:
        provider.close()


@pytest.mark.parametrize("spans", ((GhostContextSpan("ghost_experiences", 1, 10),),
    (GhostContextSpan("unknown", 0, 1),), (GhostContextSpan("ghost_experiences", -1, 2),)))
def test_stale_or_wrong_registered_spans_are_rejected(spans: tuple[GhostContextSpan, ...]) -> None:
    source = GhostContextSource("ghost_experiences", history(), 1800, "request")
    with pytest.raises(ValueError, match="registered"):
        replace_ghost_text(source.text, (source,), (), spans=spans)
