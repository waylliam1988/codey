"""Known server parser defect: protocol admission, real wire and execution."""
import json
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import NoReturn
from urllib.request import Request

import pytest

from codey.providers import api_transport, local_config, local_connection, local_selection, local_tokens
from codey.providers.local_discovery import LocalEndpoint
from codey.providers.local_tool_compat import safe_native_tools
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.observe.events import RunEvent
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object

MODEL = 'koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M'
BASE = 'http://fixture/v1'
VERSION: dict[str, object] = {'result': 'KoboldCpp', 'version': '1.117.1'}


@pytest.mark.parametrize('metadata,model,protocol,requested,base,expected', [
    (VERSION, MODEL, 'openai-completions', True, BASE, False),
    (VERSION, 'Gemma-4-Queen-31B', 'openai-completions', True, BASE, False),
    ({**VERSION, 'version': '1.117.2'}, MODEL, 'openai-completions', True, BASE, True),
    ({**VERSION, 'version': '1.117.0'}, MODEL, 'openai-completions', True, BASE, True),
    ({**VERSION, 'result': 'other'}, MODEL, 'openai-completions', True, BASE, True),
    ({}, MODEL, 'openai-completions', True, BASE, True),
    (VERSION, 'Qwen3.8-27B', 'openai-completions', True, BASE, True),
    (VERSION, 'Gemma3-12B', 'openai-completions', True, BASE, True),
    (VERSION, MODEL, 'openai-responses', True, BASE, True),
    (VERSION, MODEL, 'openai-completions', False, BASE, False),
    (VERSION, MODEL, 'openai-completions', True, 'http://proxy/custom/v1', True),
])
def test_workaround_is_limited_to_reproduced_backend_and_format(
        monkeypatch: pytest.MonkeyPatch, metadata: dict[str, object], model: str, protocol: str,
        requested: bool, base: str, expected: bool) -> None:
    seen: list[tuple[str, str]] = []

    def read_metadata(url: str, key: str) -> dict[str, object]:
        seen.append((url, key))
        return metadata

    monkeypatch.setattr(local_selection, '_metadata_json', read_metadata)
    assert safe_native_tools(base, model, api_protocol=protocol, requested=requested, api_key='test-key') is expected
    assert all(url == 'http://fixture/api/extra/version' and key == 'test-key' for url, key in seen)


def configured(monkeypatch: pytest.MonkeyPatch, thinking: bool | None = True,
               base: str = BASE) -> local_config.LocalProviderConfig:
    saved = local_config.LocalProviderConfig(base_url=base, model=MODEL, native_tools_mode='on',
        connection_revision='fixture', thinking_enabled=thinking,
        context=local_config.LocalContextBudget(32768, 8192, 12000))
    monkeypatch.setenv('NATIVE_TOOLS', '1')
    monkeypatch.setattr(local_config, 'load_local_config', lambda: saved)
    monkeypatch.setattr(local_selection, 'load_local_config', lambda: saved)
    def metadata(*args: object) -> dict[str, object]:
        return VERSION

    def endpoint(**kwargs: object) -> LocalEndpoint:
        return LocalEndpoint(base, (MODEL,))

    def budget(base_url: str, api_key: str, protocol: str,
               budget: local_config.LocalContextBudget) -> tuple[str, local_config.LocalContextBudget]:
        return 'estimated', budget

    monkeypatch.setattr(local_selection, '_metadata_json', metadata)
    monkeypatch.setattr('codey.providers.local_discovery.resolve_local_endpoint', endpoint)
    monkeypatch.setattr(local_tokens, 'admitted_context_budget', budget)
    return saved


def test_capture_freezes_safe_protocol_without_changing_preferences_or_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = configured(monkeypatch)
    selection = local_connection.capture_selection()
    assert not selection.native_tools and selection.stream
    assert selection.text_tool_format == 'plain' and selection.thinking_enabled is True
    assert local_config.load_local_config() is saved and saved.native_tools_mode == 'on'
    serialized: object = selection.to_payload()
    restored = ApiRunSelection.from_payload(decode_json_object(serialized))
    def unexpected_metadata(*args: object) -> NoReturn:
        pytest.fail('frozen text mode does not probe')

    monkeypatch.setattr(local_selection, '_metadata_json', unexpected_metadata)
    provider = local_connection.open_selection(restored)
    try:
        assert provider.native_tools is False and provider.thinking_enabled is True and provider.stream
    finally:
        provider.close()


def test_old_unsafe_native_selection_is_not_silently_changed_mid_run(monkeypatch: pytest.MonkeyPatch) -> None:
    configured(monkeypatch)
    selection = replace(local_connection.capture_selection(), native_tools=True)
    with pytest.raises(ValueError, match='saved protocol was not changed and no request was sent'):
        local_connection.open_selection(selection)


def test_old_text_selection_keeps_its_frozen_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    configured(monkeypatch)
    selection = replace(local_connection.capture_selection(), stream=False)
    serialized: object = selection.to_payload()
    provider = local_connection.open_selection(ApiRunSelection.from_payload(decode_json_object(serialized)))
    try:
        assert not provider.native_tools and not provider.stream
    finally:
        provider.close()


def test_text_fallback_timeout_closes_stream_and_discards_partial_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.providers.api_transport import GenerationUnknownError

    disconnected = threading.Event()
    received: list[JsonObject] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(parse_json_object(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            # A truncated edit frame must never become committed history.
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"{\\"tool\\":\\"edit\\"')
            self.wfile.flush()
            connection: object = self.connection
            assert isinstance(connection, socket.socket)
            connection.settimeout(3)
            if connection.recv(1) == b'':
                disconnected.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        configured(monkeypatch, False, base=f'http://127.0.0.1:{server.server_port}/v1')
        provider = local_connection.open_selection(local_connection.capture_selection())
        try:
            with pytest.raises(GenerationUnknownError):
                provider.send('Edit app.py only after reading it.', timeout=0.5)
            assert disconnected.wait(2), 'backend did not observe client disconnect'
            assert not provider.has_transport_history
            assert len(received) == 1 and received[0]['stream'] is True
            assert 'tools' not in received[0]
        finally:
            provider.close()
            server.shutdown()
            thread.join(2)


@pytest.mark.parametrize('thinking', [False, None])
def test_no_thinking_uses_existing_json_constraints_at_admission_and_direct_connect(
        monkeypatch: pytest.MonkeyPatch, thinking: bool | None) -> None:
    saved = configured(monkeypatch, thinking)
    selection = local_connection.capture_selection()
    assert not selection.native_tools and selection.stream
    assert selection.text_tool_format == 'json_schema' and selection.thinking_enabled is False
    assert saved.thinking_enabled is thinking
    for provider in (local_connection.connect_local(), local_connection.open_selection(selection)):
        try:
            assert not provider.native_tools and provider.text_tool_format == 'json_schema' and provider.stream
            assert provider.thinking_enabled is False
        finally:
            provider.close()


def test_live_gate_uses_the_same_workaround_instead_of_bypassing_local_admission(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.local_model_gate_attempts import GateTarget, RecordingProvider

    configured(monkeypatch)
    target = GateTarget(BASE, MODEL, 32768, 8192, 12000, protocol='native')
    provider = RecordingProvider(target, tmp_path)
    try:
        assert not provider.native_tools
        assert provider.text_tool_format == 'json_schema' and provider.thinking_enabled is False
    finally:
        provider.close()


@pytest.mark.parametrize('thinking', [True, False, None])
def test_same_malformed_frame_is_rejected_then_real_edit_verify_done_runs_without_server_tools(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thinking: bool | None) -> None:
    from codey.agents.request import AgentRequest
    from codey.app.context import AppContext
    from codey.completion.verification_policy import discover_verification_candidates
    from codey.operations.project_adapter import run

    configured(monkeypatch, thinking)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_text('VALUE = 0\n', encoding='utf-8')
    (project / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    (project / 'test_app.py').write_text('def test_value():\n    from app import VALUE\n    assert VALUE == 1\n', encoding='utf-8')
    malformed = ('<|tool_call>call:edit{replacements:[{new_string:<|"|>VALUE = 1<|"|>,'
                 'old_string:<|"|>VALUE = 0<|"|>},path:<|"|>app.py<|"|>}<tool_call|>')
    replies = iter([malformed, '{"tool":"read_file","args":{"path":"app.py"}}',
        '{"tool":"edit","args":{"path":"app.py","replacements":[{"old_string":"VALUE = 0","new_string":"VALUE = 1"}]}}',
        '{"tool":"run","args":{"command":"python -m pytest","path":"."}}',
        '{"tool":"done","args":{"summary":"Fixed and verified."}}'])
    wire: list[JsonObject] = []
    events: list[RunEvent] = []

    def reply(request: Request, timeout: float | None = None, **kwargs: object) -> BytesIO:
        assert isinstance(request.data, bytes)
        wire.append(parse_json_object(request.data.decode()))
        if len(wire) == 2:
            assert (project / 'app.py').read_text() == 'VALUE = 0\n'
        content = next(replies, None)
        assert content is not None, repr(events) + repr(wire[-1])
        if thinking is True and len(wire) == 2:
            content = '<|channel>thought\nInspect before changing.<channel|>' + content
        body: dict[str, object] = {'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': content}}]}
        return BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(api_transport, 'open_request', reply)
    provider = local_connection.connect_local()

    app = AppContext(tmp_path / 'state', start_scheduler=False, memory_enabled=False)
    try:
        # The adapter's durable run children need the production scheduler, not an inert memory store.
        app.durable.admit_input(conversation_id='s', request_id='input', task_id='r', text='Fix VALUE', provider_snapshot={})
        app.durable.claim_task('r')
        app.durable.start_task('r', state={})
        app.runtime_mutations.accept_operation(session_id='s', run_id='r', provider_id='local', project=project,
            turn_budget=8, max_repair_rounds=1, task_kind='project')
        app.runtime_mutations.mark_writer_running('s', 'r', provider_id='local')
        app.durable_scheduler.start()
        result = run(AgentRequest(provider=provider, project=project,
            task='Fix app.py VALUE to 1; keep tests unchanged and verify.', provider_id='local', max_turns=8,
            verification_candidates=discover_verification_candidates(project), on_event=events.append,
            run_id='r', session_id='s', durable=app.durable, runtime_mutations=app.runtime_mutations,
            managed_outputs=app.managed_outputs, workspace_revision_store=app.workspace_revisions))
    finally:
        provider.close()
        assert app.close()
    assert result.stop_reason == 'done' and result.checks_passed, events
    assert (project / 'app.py').read_text() == 'VALUE = 1\n'
    assert len(wire) == 5 and all('tools' not in item and 'tool_choice' not in item for item in wire)
    assert all(('response_format' in item) is (thinking is not True) for item in wire)
    assert any('invalid argument syntax' in json.dumps(item) for item in wire[1:])
    executed = [event.call.name for event in events
                if event.kind == 'tool' and event.call is not None and event.outcome is not None and event.outcome.ok]
    assert executed == ['read', 'edit', 'run']
