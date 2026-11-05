"""Known server parser defect: protocol admission, real wire and execution."""
import json
from dataclasses import replace

import pytest

from codey.providers import api_transport, local_config, local_connection, local_selection, local_tokens
from codey.providers.local_discovery import LocalEndpoint
from codey.providers.local_tool_compat import safe_native_tools
from codey.runtime.core.api_selection import ApiRunSelection
from tests.test_api_generation_observations_no_replay import Response

MODEL = 'koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M'
BASE = 'http://fixture/v1'
VERSION = {'result': 'KoboldCpp', 'version': '1.117.1'}


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
def test_workaround_is_limited_to_reproduced_backend_and_format(monkeypatch, metadata, model, protocol,
                                                               requested, base, expected):
    seen = []
    monkeypatch.setattr(local_selection, '_metadata_json', lambda url, key: seen.append((url, key)) or metadata)
    assert safe_native_tools(base, model, api_protocol=protocol, requested=requested, api_key='test-key') is expected
    assert all(url == 'http://fixture/api/extra/version' and key == 'test-key' for url, key in seen)


def configured(monkeypatch, thinking=True):
    saved = local_config.LocalProviderConfig(base_url=BASE, model=MODEL, native_tools_mode='on',
        connection_revision='fixture', thinking_enabled=thinking,
        context=local_config.LocalContextBudget(32768, 8192, 12000))
    monkeypatch.setenv('NATIVE_TOOLS', '1')
    monkeypatch.setattr(local_config, 'load_local_config', lambda: saved)
    monkeypatch.setattr(local_selection, 'load_local_config', lambda: saved)
    monkeypatch.setattr(local_selection, '_metadata_json', lambda *_: VERSION)
    monkeypatch.setattr('codey.providers.local_discovery.resolve_local_endpoint',
                        lambda **_: LocalEndpoint(BASE, (MODEL,)))
    monkeypatch.setattr(local_tokens, 'admitted_context_budget', lambda *args: ('estimated', args[-1]))
    return saved


def test_capture_freezes_safe_protocol_without_changing_preferences_or_thinking(monkeypatch):
    saved = configured(monkeypatch)
    selection = local_connection.capture_selection()
    assert not selection.native_tools and not selection.stream
    assert selection.text_tool_format == 'plain' and selection.thinking_enabled is True
    assert local_config.load_local_config() is saved and saved.native_tools_mode == 'on'
    restored = ApiRunSelection.from_payload(selection.to_payload())
    monkeypatch.setattr(local_selection, '_metadata_json', lambda *_: pytest.fail('frozen text mode does not probe'))
    provider = local_connection.open_selection(restored)
    try:
        assert provider.native_tools is False and provider.thinking_enabled is True
    finally:
        provider.close()


def test_old_unsafe_native_selection_is_not_silently_changed_mid_run(monkeypatch):
    configured(monkeypatch)
    selection = replace(local_connection.capture_selection(), native_tools=True)
    with pytest.raises(ValueError, match='saved protocol was not changed and no request was sent'):
        local_connection.open_selection(selection)


@pytest.mark.parametrize('thinking', [False, None])
def test_no_thinking_uses_existing_json_constraints_at_admission_and_direct_connect(monkeypatch, thinking):
    saved = configured(monkeypatch, thinking)
    selection = local_connection.capture_selection()
    assert not selection.native_tools and not selection.stream
    assert selection.text_tool_format == 'json_schema' and selection.thinking_enabled is False
    assert saved.thinking_enabled is thinking
    for provider in (local_connection.connect_local(), local_connection.open_selection(selection)):
        try:
            assert not provider.native_tools and provider.text_tool_format == 'json_schema'
            assert provider.thinking_enabled is False
        finally:
            provider.close()


def test_live_gate_uses_the_same_workaround_instead_of_bypassing_local_admission(tmp_path, monkeypatch):
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
def test_same_malformed_frame_is_rejected_then_real_edit_verify_done_runs_without_server_tools(tmp_path, monkeypatch, thinking):
    from codey.agents.request import AgentRequest
    from codey.completion.verification_policy import discover_verification_candidates
    from codey.workspace.revision import WorkspaceRevisionStore
    from tests.support.kernel_runtime import run_agent

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
    wire, events = [], []

    def reply(request, timeout=None, **kwargs):
        wire.append(json.loads(request.data))
        if len(wire) == 2:
            assert (project / 'app.py').read_text() == 'VALUE = 0\n'
        content = next(replies, None)
        assert content is not None, repr(events) + repr(wire[-1])
        if thinking is True and len(wire) == 2:
            content = '<|channel>thought\nInspect before changing.<channel|>' + content
        return Response(json.dumps({'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': content}}]}).encode())

    monkeypatch.setattr(api_transport, 'open_request', reply)
    provider = local_connection.connect_local()
    try:
        result = run_agent(AgentRequest(provider=provider, project=project,
            task='Fix app.py VALUE to 1; keep tests unchanged and verify.', provider_id='local', max_turns=8,
            verification_candidates=discover_verification_candidates(project),
            workspace_revision_store=WorkspaceRevisionStore(tmp_path / 'state'),
            on_event=events.append))
    finally:
        provider.close()
    assert result.stop_reason == 'done' and result.checks_passed
    assert (project / 'app.py').read_text() == 'VALUE = 1\n'
    assert len(wire) == 5 and all('tools' not in item and 'tool_choice' not in item for item in wire)
    assert all(('response_format' in item) is (thinking is not True) for item in wire)
    assert any('invalid argument syntax' in json.dumps(item) for item in wire[1:])
    executed = [event.call.name for event in events if event.kind == 'tool' and event.outcome.ok]
    assert executed == ['read', 'edit', 'run']
