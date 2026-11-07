"""Frozen provider requests have finite shapes before acquiring execution rights."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from codey.providers.compat_transport import execute_compat_request
from codey.providers.request_codec import decode_provider_request, encode_provider_request, provider_options_payload
from codey.providers.requests import (
    ProviderRequestOptions,
    SendTextRequest,
    SendToolResultsBoundRequest,
    SendTurnRequest,
)
from codey.providers.response_record import decode_reply

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "kernel_go_contract_v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["provider_invocations"], ids=lambda case: case["method"])
def test_request_roundtrip_dispatches_frozen_domain_fields(case):
    class Provider:
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                assert name == case["method"]
                return args, kwargs
            return invoke

    request = decode_provider_request(case)
    args, kwargs = execute_compat_request(Provider(), request)
    assert kwargs == case["kwargs"]
    if isinstance(request, (SendTextRequest, SendTurnRequest)):
        assert args[0] == request.prompt
    else:
        assert tuple(args[0]) == request.results
    if isinstance(request, SendToolResultsBoundRequest):
        assert args[1] == request.window_ref
    assert decode_provider_request(encode_provider_request(request)) == request


@pytest.mark.parametrize("args,kwargs,match", [
    ([], {}, "prompt"),
    ([123], {}, "prompt"),
    (["prompt"], {"text": "duplicate"}, "duplicate"),
    (["prompt"], {"timeout": True}, "timeout"),
    (["prompt"], {"timeout": 10**1000}, "timeout"),
    (["prompt"], {"timeout": float("nan")}, "JSON"),
    (["prompt"], {"output_token_limit": True}, "integer"),
    (["prompt"], {"output_token_limit": 2**63}, "int64"),
    (["prompt"], {"require_tool_call": True}, "unsupported"),
    (["prompt"], {"unexpected": "option"}, "unsupported"),
])
def test_invalid_text_invocations_are_rejected(args, kwargs, match):
    with pytest.raises(ValueError, match=match):
        decode_provider_request({"method": "send", "args": args, "kwargs": kwargs})


def test_explicit_presence_and_false_zero_null_survive_wire_encoding():
    kwargs = {"timeout": None, "tool_call_json": False, "output_token_limit": 0,
              "tool_call_contract": "", "tool_call_schema": None, "tool_call_results": []}
    request = decode_provider_request({"method": "send", "args": ["prompt"], "kwargs": kwargs})
    assert provider_options_payload(request.options) == kwargs
    assert encode_provider_request(request)["kwargs"] == kwargs
    absent = decode_provider_request({"method": "send", "args": ["prompt"], "kwargs": {}})
    assert provider_options_payload(absent.options) == {}


def test_keyword_native_arguments_decode_to_the_same_domain_operation():
    case = VECTORS["provider_invocations"][1]
    positional = decode_provider_request(case)
    keyword = decode_provider_request({"method": "send_turn", "args": [],
                                      "kwargs": {"prompt": case["args"][0], "tools": case["args"][1]}})
    assert positional == keyword


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_unknown_or_noninteger_version_is_rejected(version):
    with pytest.raises(ValueError, match="version"):
        decode_provider_request({"schema_version": version, "method": "send", "args": ["prompt"], "kwargs": {}})


def test_decoded_request_detaches_mutable_schema():
    case = json.loads(json.dumps(VECTORS["provider_invocations"][1]))
    request = decode_provider_request(case)
    case["args"][1][0]["parameters"]["properties"]["path"]["type"] = "number"
    assert encode_provider_request(request)["args"] != case["args"]


def test_typed_options_do_not_silently_drop_nondefault_values():
    options = ProviderRequestOptions(timeout=5, tool_call_json=True)
    assert provider_options_payload(options) == {"timeout": 5, "tool_call_json": True}


@pytest.mark.parametrize("field,value", [("finish", "unknown"), ("text", False), ("raw", []),
                                        ("raw", {"bad": float("nan")}), ("reasoning", 12)])
def test_native_reply_codec_rejects_invalid_nested_or_structural_fields(field, value):
    payload = {**VECTORS["replies"][1], field: value}
    with pytest.raises(ValueError):
        decode_reply(payload)


def test_malformed_frozen_request_never_acquires_send_rights(tmp_path):
    from codey.operations.kernel_driver import drive_provider_request
    from codey.operations.provider_request import prepare_provider_request
    from codey.runtime.effects.effect_records import RuntimeEffectError
    from tests.durable.test_provider_response_receipts import runtime

    durable, _, _, sink = runtime(tmp_path)
    provider = Mock(send=Mock(return_value="must not send"))
    try:
        reference = prepare_provider_request(sink, "send", (123,), {}, counters=None)
        with pytest.raises(RuntimeEffectError, match="prompt"):
            drive_provider_request(sink, provider, reference)
        provider.send.assert_not_called()
        assert not durable.effects("r")
    finally:
        durable.close()
