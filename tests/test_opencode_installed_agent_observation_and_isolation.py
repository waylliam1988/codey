"""Installed OpenCode observations preserve native facts and local isolation."""
import json
from http.client import RemoteDisconnected
from urllib.error import URLError

import pytest

from tests.manual.agent_stability_measurements import paired_summary, stored_output_was_read
from tests.manual.opencode_installed_agent_worker import (
    LocalApiClient,
    installed_identity,
    local_api_url,
    local_config,
    native_turn_is_terminal,
    observe_messages,
    sidecar_ready,
    terminal_observation,
)


def test_local_api_reuses_connection_and_never_retries_post(monkeypatch, tmp_path):
    class Response:
        status = 200
        reason = 'OK'
        will_close = False

        def getheaders(self):
            return [('Content-Type', 'application/json')]

        def read(self):
            return b'{"ok": true}'

    class Connection:
        instances = []

        def __init__(self, host, port, timeout):
            self.requests = []
            self.closed = False
            self.__class__.instances.append(self)

        def request(self, method, target, body=None, headers=None):
            self.requests.append((method, target, body, headers))
            if method == 'POST':
                raise RemoteDisconnected('request outcome is unknown')

        def getresponse(self):
            return Response()

        def close(self):
            self.closed = True

    monkeypatch.setattr('tests.manual.opencode_installed_agent_worker.HTTPConnection', Connection)
    client = LocalApiClient('http://127.0.0.1:8123', tmp_path)
    assert client.request('/global/health') == {'ok': True}
    assert client.request('/global/health') == {'ok': True}
    assert len(Connection.instances) == 1
    assert len(Connection.instances[0].requests) == 2
    try:
        client.request('/session', 'POST', {})
    except URLError as exc:
        assert isinstance(exc.reason, RemoteDisconnected)
    else:
        raise AssertionError('POST must propagate an uncertain outcome')
    assert len(Connection.instances) == 1
    client.close()


def test_pairing_names_opencode_without_substituting_pi():
    rows = [dict(arm=arm, seed=61, case='repair', status='completed',
                 metrics={'scenario_success': ok}) for arm, ok in [('codey', True), ('opencode', False)]]
    summary = paired_summary(rows, opponent='opencode')
    assert summary['comparable_pairs'] == 1
    assert summary['outcomes'] == {'codey_only': 1}


def test_configuration_is_local_and_sampling_precedes_model_admission():
    config = local_config('http://127.0.0.1:8000', 'fixture', 61, 0, 2048, 32768)
    assert config['enabled_providers'] == ['kobold']
    model = config['provider']['kobold']['models']['fixture']
    assert model['limit'] == {'context': 32768, 'output': 2048}
    assert model['options']['seed'] == 61
    assert config['agent']['build']['temperature'] == 0
    assert 'prompt' not in config['agent']['build']
    assert config['provider']['kobold']['options']['baseURL'] == 'http://127.0.0.1:8000/v1'


@pytest.mark.parametrize('url', ['https://api.example.org', 'http://example.org', 'http://127.0.0.1.evil/'])
def test_remote_provider_address_is_rejected(url):
    with pytest.raises(ValueError):
        local_config(url, 'fixture', 61, 0, 2048, 32768)


def test_native_tool_transitions_are_observed_once_and_keep_output():
    seen = set()
    part = {'type': 'tool', 'callID': 'call-one', 'tool': 'bash',
            'state': {'status': 'running', 'input': {'command': 'python -m unittest'}}}
    messages = [{'info': {'id': 'assistant-one', 'role': 'assistant'}, 'parts': [part]}]
    rows = observe_messages(messages, seen)
    assert rows[0]['type'] == 'tool_started' and rows[0]['args']['command'] == 'python -m unittest'
    assert observe_messages(messages, seen) == []
    part['state'] = {**part['state'], 'status': 'completed', 'output': 'tests failed', 'metadata': {'exit': 1}}
    rows = observe_messages(messages, seen)
    assert rows[0]['type'] == 'tool_finished'
    assert rows[0]['ok'] is True  # Tool delivery succeeded; a test's exit code is separate.
    assert rows[0]['result']['metadata']['exit'] == 1
    assert observe_messages(messages, seen) == []


@pytest.mark.parametrize('finish,error,reason', [('stop', None, 'done'), ('length', None, 'length'),
                                               ('stop', {'name': 'APIError'}, 'error')])
def test_terminal_projection_uses_actual_finish_and_error(finish, error, reason):
    info = {'role': 'assistant', 'finish': finish, 'time': {'completed': 42}}
    if error:
        info['error'] = error
    row = terminal_observation([{'info': info, 'parts': [{'type': 'text', 'text': 'Done'}]}])
    assert row['stop_reason'] == reason
    assert row['native_finish'] == finish


def test_missing_terminal_assistant_cannot_become_completion():
    row = terminal_observation([{'info': {'role': 'user'}, 'parts': []}])
    assert row['stop_reason'] == 'unknown'


def test_installed_identity_does_not_claim_matching_reference_commit(tmp_path):
    (tmp_path / 'resources').mkdir()
    (tmp_path / 'OpenCode.exe').write_bytes(b'executable')
    (tmp_path / 'resources/app.asar').write_bytes(b'bundle')
    identity = installed_identity(tmp_path, '1.18.35')
    assert identity['version'] == '1.18.35'
    assert identity['matching_source_commit'] is None
    assert len(identity['bundle_sha256']) == 64
    json.dumps(identity)


def test_global_health_does_not_admit_a_project_location(tmp_path):
    assert local_api_url('http://127.0.0.1:8000', '/global/health', tmp_path) == 'http://127.0.0.1:8000/global/health'
    assert 'directory=' in local_api_url('http://127.0.0.1:8000', '/session', tmp_path)


def test_startup_waits_for_the_native_ready_message(tmp_path):
    path = tmp_path / 'server.log'
    assert sidecar_ready(path) is False
    path.write_text('server starting\n', encoding='utf-8')
    assert sidecar_ready(path) is False
    path.write_text('{"type":"ready"}\n', encoding='utf-8')
    assert sidecar_ready(path) is True


def test_native_startup_error_is_not_a_failed_agent_task(tmp_path):
    path = tmp_path / 'server.log'
    path.write_text('{"type":"error","error":{"message":"missing module"}}\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='missing module'):
        sidecar_ready(path)


def test_async_prompt_admission_is_not_agent_completion():
    assert native_turn_is_terminal([{'info': {'role': 'user'}, 'parts': []}]) is False
    pending = {'info': {'role': 'assistant', 'finish': 'tool-calls', 'time': {'completed': 42}}, 'parts': []}
    assert native_turn_is_terminal([pending]) is False
    pending['info']['finish'] = 'stop'
    assert native_turn_is_terminal([pending]) is True


def test_stored_output_read_uses_opencode_native_file_path_argument():
    def records(path):
        return [{'request': {'messages': [
            {'role': 'assistant', 'tool_calls': [{'id': 'read-one', 'function': {
                'name': 'read', 'arguments': json.dumps({'filePath': path})}}]},
            {'role': 'tool', 'tool_call_id': 'read-one', 'content': 'REQUIRED_VALUE=river'}]}}]
    assert stored_output_was_read(records('archive/tool_output_123'), 'REQUIRED_VALUE=river') is True
    assert stored_output_was_read(records('test_app.py'), 'REQUIRED_VALUE=river') is False
