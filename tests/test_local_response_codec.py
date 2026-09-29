from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from codey.operations.kernel_protocol import normalize_turn
from codey.operations.kernel_transport import call_provider_send
from codey.providers.base import AssistantTurn
from codey.providers.local_openai import LocalOpenAIProvider
from codey.providers.local_response_codec import normalize_local_reply, parse_local_tool_markup


def test_local_gemma_frame_normalizes_to_standard_tool_call() -> None:
    turn = normalize_local_reply(
        '<|tool_call>call:tool:read_file{args:{path:"app.py"}}<tool_call|>'
    )

    assert isinstance(turn, AssistantTurn)
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"path": "app.py"}
    assert turn.tool_calls[0].id.startswith("local-")


def test_local_gemma_frame_accepts_direct_argument_object() -> None:
    call = parse_local_tool_markup(
        '<|tool_call>call:tool:read_file{path:"app.py"}<tool_call|>'
    )

    assert call is not None
    assert call.arguments == {"path": "app.py"}


def test_local_gemma_incomplete_frame_stays_plain_text() -> None:
    reply = '<|tool_call>call:tool:read_file{args:{path:"app.py"}}'

    assert normalize_local_reply(reply) == reply


def test_local_gemma_invalid_literal_stays_plain_text() -> None:
    reply = '<|tool_call>call:tool:read_file{args:{path:__import__("os")}}<tool_call|>'

    assert normalize_local_reply(reply) == reply


def test_local_provider_normalize_reply_returns_standard_turn() -> None:
    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="gemma-test")

    turn = provider.normalize_reply(
        '<|tool_call>call:tool:read_file{args:{path:"app.py"}}<tool_call|>'
    )

    assert isinstance(turn, AssistantTurn)
    assert turn.tool_calls[0].name == "read_file"


def test_kernel_transport_uses_provider_normalize_hook() -> None:
    provider = SimpleNamespace(
        send=lambda _prompt: '<|tool_call>call:tool:read_file{path:"app.py"}<tool_call|>',
        normalize_reply=normalize_local_reply,
    )

    reply = call_provider_send(provider, "read app.py")

    assert isinstance(reply, AssistantTurn)
    assert reply.tool_calls[0].arguments == {"path": "app.py"}


def test_kernel_transport_ignores_implicit_dynamic_normalize_attribute() -> None:
    provider = mock.Mock()
    provider.send.return_value = "plain reply"

    assert call_provider_send(provider, "continue") == "plain reply"


def test_local_codec_output_still_uses_kernel_toolspec_and_policy_validation() -> None:
    from types import SimpleNamespace

    turn = normalize_local_reply(
        '<|tool_call>call:tool:not_a_real_tool{args:{path:"app.py"}}<tool_call|>'
    )

    plan = normalize_turn(
        turn,
        policy=SimpleNamespace(allows=lambda _grant: True),
    )

    assert plan.calls == []
    assert plan.protocol_error
