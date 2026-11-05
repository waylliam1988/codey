"""Production Cline factories, actual HTTP generation, history and kernel tools."""
import io
import json
import threading
import time
import urllib.error
from contextlib import closing

import pytest

from codey.providers import api_context, api_generation, api_transport
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.cline import connection
from codey.providers.cline.codec import ChatCodec
from codey.providers.cline.stream import ClineAccumulator
from codey.runtime.core import cancellation
from tests.support.cline import MODEL, Response, completion, credential, resource


@pytest.fixture
def connected(tmp_path, monkeypatch):
    instance = resource(tmp_path / "connector")
    monkeypatch.setattr(connection, "runtime", lambda: instance)
    provider = connection.open_selection(connection.capture_selection({"model": MODEL}))
    provider.bind_usage("cline", lambda _: None)
    yield instance, provider
    provider.close()
    instance.close()


def install_transport(monkeypatch, replies):
    sent = []
    replies = iter(replies)

    def open_request(request, timeout, **kwargs):
        sent.append((json.loads(request.data), dict(request.header_items()), timeout))
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(api_transport, "open_request", open_request)
    return sent


def test_nonstream_success_data_envelope_keeps_two_turn_history_and_usage(connected, monkeypatch):
    instance, provider = connected
    provider.stream = False
    usage = []
    provider.bind_usage("cline", usage.append)
    sent = install_transport(monkeypatch, [Response({"success": True, "data": completion()}),
                                          Response({"success": True, "data": completion()})])
    assert provider.send("first") == provider.send("second") == "received"
    semantic = [row for row in sent[1][0]["messages"] if not str(row.get("content", "")).startswith("<request_context>")]
    assert [row["role"] for row in semantic] == ["user", "assistant", "user"]
    assert len(provider.context_ledger.view) == 4 and instance.active == 0
    assert all(row.usage.input_tokens == 20 and row.usage.output_tokens == 8 for row in usage)
    assert all(row[1]["Authorization"] == "Bearer workos:ACCESS_MARKER" for row in sent)
    assert "ACCESS_MARKER" not in json.dumps(provider.context_ledger.view)
    assert not provider.api_key and not provider.system_prompt


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_effort_compiles_exact_gateway_fields(connected, monkeypatch, effort):
    _, provider = connected
    provider.reasoning_effort = effort
    sent = install_transport(monkeypatch, [Response(completion())])
    provider.send("hello")
    payload = sent[0][0]
    assert "reasoning_effort" not in payload and "chat_template_kwargs" not in payload
    assert payload.get("reasoning") == ({"effort": effort} if effort else None)


@pytest.mark.parametrize("thinking", [None, False, True])
def test_toggle_default_is_preserved_on_wire(connected, monkeypatch, thinking):
    instance, original = connected
    raw = instance.catalog.data["capabilities"]["upstage/solar-mini4"]
    raw["reasoning"].pop("supported_efforts")
    selection = connection.capture_selection({"model": MODEL, "thinking": thinking})
    provider = connection.open_selection(selection)
    sent = install_transport(monkeypatch, [Response(completion())])
    try:
        provider.send("hello")
        assert sent[0][0].get("reasoning") == ({"enabled": thinking} if thinking is not None else None)
    finally:
        provider.close()


def test_full_401_refreshes_once_with_same_body_and_logical_task_id(connected, monkeypatch):
    instance, provider = connected
    refusal = urllib.error.HTTPError("https://api.cline.bot", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"ACCESS_MARKER"}'))
    sent = install_transport(monkeypatch, [refusal, Response(completion())])
    assert provider.send("hello") == "received"
    assert len(sent) == 2 and sent[0][0] == sent[1][0]
    assert sent[0][1]["X-task-id"] == sent[1][1]["X-task-id"]
    assert sent[0][1]["Authorization"] == "Bearer workos:ACCESS_MARKER"
    assert sent[1][1]["Authorization"] == "Bearer workos:ACCESS_ROTATED"
    assert instance.auth.store.load().token_version == 1 and instance.active == 0


def test_repeated_401_does_not_loop_or_leak_reflected_credentials(connected, monkeypatch):
    instance, provider = connected
    refusals = [urllib.error.HTTPError("https://api.cline.bot", 401, "Unauthorized", {},
        io.BytesIO(b'{"error":{"message":"ACCESS_MARKER REFRESH_MARKER"}}')) for _ in range(2)]
    sent = install_transport(monkeypatch, refusals)
    with pytest.raises(api_transport.GenerationRejectedError) as error:
        provider.send("hello")
    assert "MARKER" not in str(error.value) and len(sent) == 2
    assert instance.active == 0 and not provider.context_ledger.view
    assert instance.auth.snapshot()["state"] == "reauth_required"


def test_wrapped_upstream_invalid_request_is_not_retried(connected, monkeypatch):
    instance, provider = connected
    refusal = urllib.error.HTTPError("https://api.cline.bot", 500, "Failed", {},
        io.BytesIO(b'{"error":{"message":"upstream 400 Invalid request ACCESS_MARKER"}}'))
    sent = install_transport(monkeypatch, [refusal])
    with pytest.raises(api_transport.GenerationRejectedError) as error:
        provider.send("hello")
    assert len(sent) == 1 and error.value.error_code == "upstream_invalid_request"
    assert "ACCESS_MARKER" not in str(error.value) and instance.active == 0


def test_account_switch_is_rejected_before_any_inference(connected, monkeypatch):
    instance, provider = connected
    instance.auth.store.save(credential(epoch="account-B"))
    sent = install_transport(monkeypatch, [])
    with pytest.raises(RuntimeError, match="account changed"):
        provider.send("hello")
    assert not sent and instance.active == 0


def test_logout_is_refused_until_request_settles(connected, monkeypatch):
    instance, provider = connected

    def generate(*args, **kwargs):
        assert instance.active == 1
        with pytest.raises(RuntimeError, match="Stop tasks"):
            instance.action("logout", None)
        return completion()

    monkeypatch.setattr(api_generation, "generate", generate)
    provider.send("hello")
    assert instance.active == 0
    instance.action("logout", None)
    assert instance.auth.store.load() is None
    with pytest.raises(RuntimeError):
        provider.send("stale provider")


def test_auxiliary_preserves_codec_policy_dynamic_auth_and_cancellation(connected, monkeypatch):
    instance, provider = connected
    child = api_context.fork_auxiliary(provider)
    try:
        assert child._codec is provider._codec and child.codec_revision == provider.codec_revision
        assert child.request_context_headers is provider.request_context_headers
        assert child.request_policy is provider.request_policy
        assert child.connection_revision == provider.connection_revision
        instance.auth.store.save(credential(version=2))
        sent = install_transport(monkeypatch, [Response(completion())])
        child.send("summarize")
        assert sent[0][1]["Authorization"] == "Bearer workos:ACCESS_MARKER"
        provider.abandon_inflight()
        with pytest.raises(RuntimeError):
            child.send("cancelled auxiliary")
    finally:
        child.close()


def test_real_summary_uses_same_connector_without_storing_opaque_data_in_text(connected, monkeypatch):
    instance, provider = connected
    sent = install_transport(monkeypatch, [Response(completion({"content": "Goal: keep API; next: run tests."}))])
    summary = api_context.summarize_context(provider, [{"role": "user", "content": "Preserve the API"}], time.monotonic() + 5)
    assert "keep API" in summary and instance.active == 0
    assert sent[0][1]["Authorization"] == "Bearer workos:ACCESS_MARKER"
    assert not provider.context_ledger.view


def test_stream_interleaves_text_tools_and_reasoning_signatures(connected, monkeypatch):
    _, provider = connected
    frames = [
        {"content": "Checking", "reasoning_details": [{"index": 0, "id": "r1", "type": "reasoning.text", "text": "Think"}]},
        {"tool_calls": [{"index": 0, "id": "read-1", "function": {"name": "read_file", "arguments": '{"path":'}}],
         "reasoning_details": [{"index": 1, "id": "r2", "type": "reasoning.encrypted", "data": "opaque-", "signature": "sig-"}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}],
         "reasoning_details": [{"id": "r1", "text": " carefully"}, {"id": "r2", "data": "tail", "signature": "end"}]},
    ]
    events = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in frames]
    events += [{"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
               {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 8}}]
    raw = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
    sent = install_transport(monkeypatch, [Response(raw, sse=True), Response(completion())])
    tools = [ProviderToolDefinition("read_file", "Read", {"type": "object", "properties": {"path": {"type": "string"}}})]
    turn = provider.send_turn("read", tools)
    assert turn.reasoning == "Think carefully" and turn.tool_calls[0].id == "read-1"
    assert "opaque" not in turn.reasoning and "sig-" not in turn.reasoning
    provider.send_tool_results([ProviderToolResult("read-1", "contents")], tools)
    message = next(row for row in sent[1][0]["messages"] if row["role"] == "assistant")
    assert message["reasoning_details"][1]["data"] == "opaque-tail"
    assert message["reasoning_details"][1]["signature"] == "sig-end"
    provider._codec.validate_view(provider.context_ledger.view)


def test_no_done_frame_is_unknown_and_commits_no_history(connected, monkeypatch):
    instance, provider = connected
    raw = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"stop"}]}\n\n'
    sent = install_transport(monkeypatch, [Response(raw, sse=True)])
    with pytest.raises(api_transport.GenerationUnknownError):
        provider.send("hello")
    assert len(sent) == 1 and not provider.context_ledger.view and instance.active == 0


def test_stream_error_redacts_reflected_credentials_and_is_not_retried(connected, monkeypatch):
    instance, provider = connected
    raw = b'data: {"error":{"message":"ACCESS_MARKER REFRESH_MARKER"}}\n\n'
    sent = install_transport(monkeypatch, [Response(raw, sse=True)])
    with pytest.raises(RuntimeError) as error:
        provider.send("hello")
    assert "MARKER" not in str(error.value) and len(sent) == 1
    assert not provider.context_ledger.view and instance.active == 0


def test_cancellation_during_decode_rejects_late_commit(connected, monkeypatch):
    _, provider = connected
    stop = threading.Event()
    original = provider._codec.decode_exchange

    def decode(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(provider._codec, "decode_exchange", decode)
    install_transport(monkeypatch, [Response(completion())])
    with cancellation.scope(stop), pytest.raises(api_transport.GenerationUnknownError):
        provider.send("hello")
    assert not provider.context_ledger.view


def test_nonzero_cost_blocks_next_admission_without_falsifying_tokens(connected, monkeypatch):
    instance, provider = connected
    body = completion()
    body["usage"]["cost"] = 0.01
    sent = install_transport(monkeypatch, [Response(body)])
    provider.send("hello")
    assert instance.blocked and instance.payload()["state"] == "blocked"
    with pytest.raises(RuntimeError, match="blocked"):
        provider.send("must not send")
    assert len(sent) == 1


@pytest.mark.parametrize("case", ["edit", "shell", "read"])
def test_real_kernel_workspace_tools_and_shell_approval(connected, monkeypatch, tmp_path, case):
    from codey.app.context import AppContext
    from tests.support.workspace_conversation import short_test_state_home
    from tests.test_chat_workspace_tools_and_answer_completion import execute_frame, workspace_frame

    instance, provider = connected
    name, arguments = {"edit": ("edit", {"path": "result.py", "content": "value = 42\n"}),
                       "shell": ("shell", {"path": ".", "command": "python --version"}),
                       "read": ("read_file", {"path": "input.py"})}[case]
    (tmp_path / "input.py").write_text("SOURCE_MARKER\n", encoding="utf8")
    call = {"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    sent = install_transport(monkeypatch, [Response(completion({"content": "", "tool_calls": [call]})),
                                          Response(completion({"content": "Finished."}))])
    frame = workspace_frame(tmp_path, provider, task="Do the requested action", denied_capabilities=("project.verify",))
    with closing(AppContext(short_test_state_home())) as state:
        result, events = execute_frame(frame, state)
    assert result.event["stop_reason"] == ("approval" if case == "shell" else "done"), result.event
    if case == "edit":
        assert (tmp_path / "result.py").read_text(encoding="utf8") == "value = 42\n"
    if case == "read":
        assert "SOURCE_MARKER" in str(sent[1][0]["messages"])
    if case == "shell":
        assert any(getattr(event, "command", "") == "python --version" for event in events)
    assert instance.active == 0


def test_unknown_reasoning_contract_and_multiple_choices_fail_closed():
    codec = ChatCodec()
    for body in (completion({"content": "x", "reasoning_details": [{"type": "unknown", "data": "secret"}]}),
                 {"choices": [{}, {}]}):
        with pytest.raises(ValueError):
            codec.decode_exchange(body, text_only=True, text_decoder=None)
    accumulator = ClineAccumulator()
    with pytest.raises(ValueError, match="ambiguous"):
        accumulator.add_delta({"reasoning_details": [{"type": "reasoning.text", "text": "no identity"}]})


def test_opaque_reasoning_never_crosses_canonical_context_replay():
    result = ChatCodec.encode_context([{"role": "assistant", "content": "Fact", "reasoning_details": [
        {"type": "reasoning.encrypted", "data": "opaque"}]}])
    assert result == [{"role": "assistant", "content": "Fact"}]


def test_protocol_ledger_disk_roundtrip_preserves_signatures_but_portable_replay_drops_them(
    connected, monkeypatch, tmp_path,
):
    from codey.providers.context_ledger import ContextLedger

    _, provider = connected
    provider.context_ledger = ContextLedger(tmp_path / "ledger", identity="protocol:chat")
    details = [{"type": "reasoning.encrypted", "id": "r-1", "data": "OPAQUE_MARKER", "signature": "SIGNATURE_MARKER"}]
    install_transport(monkeypatch, [Response(completion({"content": "A portable fact", "reasoning_details": details}))])
    assert provider.send("remember") == "A portable fact"
    restored = ContextLedger(tmp_path / "ledger", identity="protocol:chat").restore()
    assert restored[-1]["reasoning_details"] == details
    provider._codec.validate_view(restored)
    replay = provider._codec.encode_context(restored)
    assert replay[-1]["content"] == "A portable fact"
    assert "OPAQUE_MARKER" not in json.dumps(replay) and "SIGNATURE_MARKER" not in json.dumps(replay)
