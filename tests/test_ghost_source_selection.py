"""Identity dedup, atomic budgets and send-time snapshot authority."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.ghost.observation_index import history_refs, render_retrieved_block
from codey.operations.kernel_actions import _bind_attempt_gate
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.provider_session import adapt_provider
from codey.operations.represented_history import represented_history_refs, represented_inputs
from codey.providers.api_provider import ApiProvider
from codey.providers.attempt_gate import AttemptGateCallback, AttemptGateView
from codey.types.json_value import JsonObject
from codey.workspace.context_admission import GhostAdmissionError, GhostContextSource
from codey.workspace.ghost_selection import GhostContextSpan, replace_ghost_text, select_ghost_sources


def history(identity: str = "source", *, assistant: str = "I suggest quartzstore; not yet verified.") -> str:
    return render_retrieved_block([{"run_id": identity, "mode": "project", "ts": "2026-11-18T00:00:00Z",
        "user_text": "Only today, do not decide for me.", "assistant_text": assistant}])


def source(key: str, text: str, budget: int = 1800) -> GhostContextSource:
    return GhostContextSource(key, text, budget, "request")


def test_identity_includes_run_role_and_exact_content() -> None:
    text = history()
    assert len(history_refs(text)) == 2
    assert not history_refs(text).intersection(history_refs(history("other")))
    assert len(history_refs(text).intersection(history_refs(history(assistant="Different suggestion.")))) == 1
    assert not history_refs(text.replace("Only today", "Always"))


def test_selection_only_omits_actually_represented_role_and_keeps_negation() -> None:
    text = history()
    request_only = render_retrieved_block([{"run_id": "source", "mode": "project", "ts": "2026-11-18T00:00:00Z",
        "user_text": "Only today, do not decide for me.", "assistant_text": ""}], view="requests")
    original = (source("ghost_experiences", text),)
    selected = select_ghost_sources(original, represented=history_refs(request_only))
    assert len(selected) == 1
    assert '"user":' not in selected[0].text and '"assistant":' in selected[0].text
    assert "not yet verified" in selected[0].text
    assert select_ghost_sources(original, represented=history_refs(text)) == ()
    assert select_ghost_sources(original) == original


@pytest.mark.parametrize("budget", (0, 300, 700, 1000, 4000, 6000))
def test_total_budget_preserves_atomic_goal_pair_and_whole_requirements(budget: int) -> None:
    goal = source("ghost_confirmed_project_goal", "Only when exploring options, do not decide for me.")
    prior = source("ghost_project_goal_history", "Confirmed by actual user review; suggestion remains unverified.")
    sources = (source("ghost_directive", "Use concise replies."), source("ghost_experiences", history()), prior, goal)
    selected = select_ghost_sources(sources, budget=budget)
    assert sum(len(row.text) for row in selected) + 2 * max(0, len(selected) - 1) <= budget
    keys = {row.key for row in selected}
    assert (prior.key in keys) == (goal.key in keys)
    if goal.key in keys:
        assert goal in selected and prior in selected
    spans: list[GhostContextSpan] = []
    offset = 0
    for row in sources:
        spans.append(GhostContextSpan(row.key, offset, offset + len(row.text)))
        offset += len(row.text) + 2
    text = replace_ghost_text("\n\n".join(row.text for row in sources), sources, selected, spans=tuple(spans))
    assert all(row.text in text and len(row.text) <= row.budget for row in selected)
    assert select_ghost_sources(selected, budget=budget) == selected


def test_protocol_view_is_required_not_session_or_exported_summary() -> None:
    class SummaryOnly:
        def export_work_state(self) -> str:
            return history()

    assert not represented_history_refs(SummaryOnly())
    provider = ApiProvider("http://127.0.0.1:1/v1", "fixture")
    try:
        messages: list[dict[str, object]] = [{"role": "user", "content": history()}]
        provider.load_durable_context(messages, source_entry_ids=("actual",))
        assert represented_history_refs(provider) == history_refs(history())
        provider.new_chat()
        assert not represented_history_refs(provider)
    finally:
        provider.close()


@pytest.mark.parametrize("protocol", ("openai-completions", "openai-responses"))
def test_original_user_input_requires_canonical_id_and_current_complete_bytes(protocol: str) -> None:
    provider = ApiProvider("http://127.0.0.1:1/v1", "fixture", api_protocol=protocol)
    text = "Only today, do not decide for me."
    original = (source("ghost_experiences", history()),)

    def canonical_id(run: str) -> str:
        return "canonical-input" if run == "source" else ""

    try:
        messages: list[dict[str, object]] = [{"role": "user", "content": text}]
        provider.load_durable_context(messages, source_entry_ids=("wrong-input",))
        assert not represented_inputs(provider, original, canonical_id)
        provider.new_chat()
        provider.load_durable_context(messages, source_entry_ids=("canonical-input",))
        retained = represented_inputs(provider, original, canonical_id)
        assert len(retained) == 1 and retained[0][1] == text
        selected = select_ghost_sources(original, represented=frozenset(ref for ref, _text in retained))
        assert len(selected) == 1 and '"user":' not in selected[0].text
        assert '"assistant":' in selected[0].text
        messages = [{"role": "user", "content": "Summary: decide for me."}]
        provider.new_chat()
        provider.load_durable_context(messages, source_entry_ids=("canonical-input",))
        assert not represented_inputs(provider, original, canonical_id)
        provider.new_chat()
        assert not represented_inputs(provider, original, canonical_id)
    finally:
        provider.close()


def test_empty_selection_still_checks_deleted_or_compacted_source_before_send(tmp_path: Path) -> None:
    class GateCapture:
        gate: AttemptGateCallback | None = None

        def bind_attempt_gate(self, gate: AttemptGateCallback | None) -> None:
            self.gate = gate

    provider = ApiProvider("http://127.0.0.1:1/v1", "fixture")
    text = history()
    try:
        messages: list[dict[str, object]] = [{"role": "user", "content": text}]
        provider.load_durable_context(messages, source_entry_ids=("actual",))
        built = KernelRunRequestBuilder(provider, "next", "test", "fixture", "Continue.",
            context_text=text, ghost_sources=(source("ghost_experiences", text),),
            ghost_spans=(GhostContextSpan("ghost_experiences", 0, len(text)),)).build()
        assert built.transport.ghost_sources == ()
        validator = built.transport.attempt_validate_ghost_sources
        assert validator is not None and validator(())
        capture = GateCapture()
        assert _bind_attempt_gate(adapt_provider(capture), built.transport, None)
        payload: JsonObject = {"messages": [{"role": "user", "content": "Continue."}]}
        gate = capture.gate
        assert gate is not None
        with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
            gate(AttemptGateView("conversation", "Continue."), payload)
        with pytest.raises(GhostAdmissionError, match="represented_history_missing"):
            gate(AttemptGateView("compaction", "Continue."), payload)
        payload = {"messages": [{"role": "user", "content": text}]}
        gate(AttemptGateView("conversation", "Continue."), payload)
        provider.new_chat()
        assert not validator(())
        with pytest.raises(GhostAdmissionError, match="source_snapshot_changed"):
            gate(AttemptGateView("conversation", "Continue."), payload)
    finally:
        provider.close()


def test_project_validator_checks_original_snapshot_after_budget_selection() -> None:
    original = (source("ghost_experiences", history()),)
    calls: list[tuple[GhostContextSource, ...]] = []

    def valid(actual: tuple[GhostContextSource, ...]) -> bool:
        calls.append(actual)
        return actual == original

    request = KernelRunRequestBuilder(object(), "next", "test", "fixture", "Continue.",
        context_text=original[0].text, ghost_sources=original, attempt_validate_ghost_sources=valid).build()
    check = request.transport.attempt_validate_ghost_sources
    assert check is not None and check(request.transport.ghost_sources)
    assert calls == [original]
    assert not check((replace(original[0], text="different"),))


def test_expiry_and_scope_still_reach_existing_admission() -> None:
    from codey.workspace.context_admission import admit_ghost_context

    original = (replace(source("ghost_experiences", history()), expires_at="2000-01-01T00:00:00Z"),)
    selected = select_ghost_sources(original)
    with pytest.raises(GhostAdmissionError, match="expired"):
        admit_ghost_context(selected[0].text, selected, request_scope="request", require_all=True)
    wrong = (replace(original[0], expires_at="", scope="project:other"),)
    with pytest.raises(GhostAdmissionError, match="scope_mismatch"):
        admit_ghost_context(wrong[0].text, select_ghost_sources(wrong), request_scope="request", require_all=True)


def test_direct_or_restored_callers_cannot_bypass_combined_cap() -> None:
    from codey.workspace.context_admission import admit_ghost_context

    sources = (source("first", "a" * 3100, 4000), source("second", "b" * 3100, 4000))
    with pytest.raises(GhostAdmissionError, match="combined_over_budget"):
        admit_ghost_context("\n\n".join(row.text for row in sources), sources, request_scope="request", require_all=True)
