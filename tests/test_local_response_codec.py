from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from codey.operations.provider_session import normalize_provider_reply
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn
from codey.providers.local_response_codec import (
    normalize_local_reply,
    parse_local_tool_markup,
)
from tests.support.kernel_protocol_test_helpers import normalize_turn


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


def test_local_gemma_variant_frame_accepts_json_style_literals_and_escaped_quotes() -> None:
    call = parse_local_tool_markup(
        '<|tool_call>call:grep{before_context:1,include:".py",literal:true,query:<|\\"|>needle<|\\"|>}<tool_call|>'
    )

    assert call is not None
    assert call.name == "grep"
    assert call.arguments == {
        "before_context": 1,
        "include": ".py",
        "literal": True,
        "query": "needle",
    }


def test_local_gemma_native_frame_accepts_shell_style_marker_and_search_context() -> None:
    call = parse_local_tool_markup(
        "<|tool_call>call:grep{path:'.',query:<|\"|>needle<|\"|>,include:'*.{py,ts}',"
        "exclude:['vendor/**'],before_context:2,after_context:3}<tool_call|>"
    )

    assert call is not None
    assert call.name == "grep"
    assert call.arguments == {
        "path": ".", "query": "needle", "include": "*.{py,ts}",
        "exclude": ["vendor/**"], "before_context": 2, "after_context": 3,
    }


def test_local_gemma_incomplete_frame_stays_plain_text() -> None:
    reply = '<|tool_call>call:tool:read_file{args:{path:"app.py"}}'

    assert normalize_local_reply(reply) == reply


def test_local_gemma_invalid_literal_stays_plain_text() -> None:
    reply = '<|tool_call>call:tool:read_file{args:{path:__import__("os")}}<tool_call|>'

    assert normalize_local_reply(reply) == reply


def test_local_provider_normalize_reply_returns_standard_turn() -> None:
    provider = ApiProvider(base_url="http://127.0.0.1:9/v1", model="gemma-test", text_decoder=normalize_local_reply)

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

    reply = normalize_provider_reply(provider, provider.send("read app.py"))

    assert isinstance(reply, AssistantTurn)
    assert reply.tool_calls[0].arguments == {"path": "app.py"}


def test_kernel_transport_ignores_implicit_dynamic_normalize_attribute() -> None:
    provider = mock.Mock()
    provider.send.return_value = "plain reply"

    assert normalize_provider_reply(provider, provider.send("continue")) == "plain reply"


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


def test_gemma_final_decision_is_provider_metadata_only() -> None:
    turn = normalize_local_reply(
        '[TOOLCALL REASONING]: {"reasoning":"tests passed","final_decision":"yes","tool_name":"edit"}'
    )

    assert isinstance(turn, AssistantTurn)
    assert turn.tool_calls == ()
    assert turn.raw["provider_metadata"] == {
        "reasoning": "tests passed",
        "final_decision": "yes",
        "tool_name": "edit",
    }


def test_unknown_final_decision_is_not_a_tool_call() -> None:
    turn = normalize_local_reply(
        '[TOOLCALL REASONING]: {"final_decision":"maybe","tool_name":"unknown"}'
    )

    assert isinstance(turn, AssistantTurn)
    assert turn.tool_calls == ()


def test_local_frame_preserves_literal_words_and_marker_text_inside_strings():
    import json
    values = ['is true and false or null', 'literal <|"|> delimiter', 'True False None',
              'a\\b "quoted"\nnext', '中文 true null']
    for value in values:
        body = json.dumps({'query': value, 'literal': True}, ensure_ascii=False)
        call = parse_local_tool_markup('<|tool_call>call:grep' + body + '<tool_call|>')
        assert call is not None
        assert call.arguments == {'query': value, 'literal': True}


def test_local_frame_rejects_duplicate_keys_and_expression_syntax():
    for body in ('{query:"first",query:"second"}', '{query:("x",)}', '{query:"a" "b"}',
                 '{query:1+2}', '{query:float("nan")}'):
        assert parse_local_tool_markup('<|tool_call>call:grep' + body + '<tool_call|>') is None


def test_marker_string_preserves_escaped_and_unescaped_quotes():
    for value in (r'say "hi"', r'say \"hi\"'):
        call = parse_local_tool_markup('<|tool_call>call:grep{query:<|"|>' + value + '<|"|>}<tool_call|>')
        assert call is not None
        assert call.arguments['query'] == 'say "hi"'


def test_local_frame_uses_json_escape_semantics_including_slash_and_surrogate_pairs():
    call = parse_local_tool_markup(r'<|tool_call>call:grep{query:"a\/b \ud83d\ude80",literal:true}<tool_call|>')
    assert call is not None
    assert call.arguments['query'] == 'a/b \U0001f680'
    single = parse_local_tool_markup(r"<|tool_call>call:grep{query:'it\'s a/b',literal:true}<tool_call|>")
    assert single is not None and single.arguments['query'] == "it's a/b"
    assert parse_local_tool_markup(r'<|tool_call>call:grep{query:"\x41"}<tool_call|>') is None


def test_local_frame_rejects_nonfinite_values_and_unpaired_surrogates():
    for body in (r'{query:"\ud800"}', '{query:1e309}'):
        assert parse_local_tool_markup('<|tool_call>call:grep'+body+'<tool_call|>') is None
