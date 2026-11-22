"""Unusable model summaries preserve the bounded local handoff."""

import json

import pytest

from codey.agents.handoff import MAX_HANDOFF_TEXT_CHARS, ConversationContext, ConversationSnapshot, render_handoff
from codey.types.json_value import parse_json_object


@pytest.mark.parametrize("value", ["[" * 2000 + "0" + "]" * 2000, "9" * 5000],
                         ids=["deep_ignored_value", "oversized_integer"])
def test_unusable_summary_remains_bounded_and_preserves_local_facts(value: str) -> None:
    context = ConversationContext(snapshot=ConversationSnapshot(mode="chat", goal="Keep the known task"))
    reply = '{"current_state":' + value + '}'

    def summarize(_prompt: str) -> str:
        return reply

    handoff = parse_json_object(context.prepare_model_handoff(summarize))

    assert handoff["goal"] == "Keep the known task"
    summary = handoff["conversation_summary"]
    assert isinstance(summary, dict)
    state = summary["current_state"]
    assert isinstance(state, str)
    assert len(state) <= MAX_HANDOFF_TEXT_CHARS


def test_a_valid_summary_can_follow_a_malformed_fragment() -> None:
    context = ConversationContext(snapshot=ConversationSnapshot(mode="chat", goal="Keep the known task"))
    reply = '{"unknown": BROKEN} {"current_state":"ready"}'

    def summarize(_prompt: str) -> str:
        return reply

    handoff = parse_json_object(context.prepare_model_handoff(summarize))

    assert handoff["conversation_summary"] == {"current_state": "ready"}


@pytest.mark.parametrize("summary", ["not-json text", "9" * 5000], ids=["plain_text", "oversized_integer"])
def test_snapshot_preserves_unusable_serialized_summary_as_text(summary: str) -> None:
    snapshot = ConversationSnapshot(mode="chat", goal="Keep the known task", conversation_summary=summary)

    handoff = parse_json_object(render_handoff(snapshot))

    assert handoff["goal"] == "Keep the known task"
    assert handoff["conversation_summary"] == summary


@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
def test_parser_limits_allow_a_later_valid_summary(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    original = json.JSONDecoder()

    class LimitedDecoder:
        def raw_decode(self, text: str) -> tuple[object, int]:
            if text.startswith('{"limited":'):
                raise error_type("decoder resource limit")
            decoded: tuple[object, int] = original.raw_decode(text)
            return decoded
    context = ConversationContext(snapshot=ConversationSnapshot(mode="chat", goal="Keep the known task"))

    def summarize(_prompt: str) -> str:
        return '{"limited":0} {"current_state":"ready"}'

    with monkeypatch.context() as patch:
        patch.setattr(json, "JSONDecoder", LimitedDecoder)
        rendered = context.prepare_model_handoff(summarize)
    handoff = parse_json_object(rendered)

    assert handoff["goal"] == "Keep the known task"
    assert handoff["conversation_summary"] == {"current_state": "ready"}


@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
def test_snapshot_keeps_text_when_its_decoder_reaches_a_limit(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    def decode(_text: str) -> object:
        raise error_type("decoder resource limit")

    monkeypatch.setattr(json, "loads", decode)
    snapshot = ConversationSnapshot(mode="chat", conversation_summary="preserve this summary")

    assert snapshot.to_payload()["conversation_summary"] == "preserve this summary"
