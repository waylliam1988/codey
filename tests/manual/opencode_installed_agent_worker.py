"""Drive the installed desktop sidecar's native agent through its local API.

The host supplies only the Electron parent-port lifecycle. Model prompts, native
tools, permissions, compaction and retries remain in the unmodified app.asar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tests.manual.agent_stability_proxy import local_url


def installed_identity(install, version):
    install = Path(install)
    return {'version': version, 'matching_source_commit': None,
            'executable_sha256': hashlib.sha256((install / 'OpenCode.exe').read_bytes()).hexdigest(),
            'bundle_sha256': hashlib.sha256((install / 'resources/app.asar').read_bytes()).hexdigest()}


def local_config(base_url, model_id, seed, temperature, max_tokens, window):
    base_url = local_url(base_url)
    return {'enabled_providers': ['kobold'], 'model': 'kobold/' + model_id,
            'small_model': 'kobold/' + model_id, 'autoupdate': False, 'share': 'disabled',
            'provider': {'kobold': {'npm': '@ai-sdk/openai-compatible', 'name': 'KoboldCpp',
                'options': {'baseURL': base_url + '/v1', 'apiKey': 'local'},
                'models': {model_id: {'name': model_id, 'tool_call': True, 'temperature': True,
                    'limit': {'context': window, 'output': max_tokens},
                    'options': {'seed': seed, 'temperature': temperature, 'maxTokens': max_tokens}}}}},
            'agent': {'build': {'temperature': temperature}},
            'permission': {'bash': 'allow', 'edit': 'allow', 'external_directory': 'allow'},
            'compaction': {'auto': True, 'prune': True}}


def observe_messages(messages, seen):
    rows = []
    for message in messages:
        for part in message.get('parts', []):
            if part.get('type') != 'tool':
                continue
            identity = part.get('callID') or part.get('id')
            state = part.get('state', {})
            status = state.get('status')
            if not identity or status not in {'running', 'completed', 'error'}:
                continue
            started = (identity, 'started')
            if started not in seen:
                seen.add(started)
                rows.append({'type': 'tool_started', 'tool_id': identity, 'tool_name': part.get('tool'),
                             'args': state.get('input', {}), 'native_status': status})
            finished = (identity, 'finished')
            if status != 'running' and finished not in seen:
                seen.add(finished)
                rows.append({'type': 'tool_finished', 'tool_id': identity, 'tool_name': part.get('tool'),
                             'ok': status == 'completed', 'result': state, 'native_status': status})
    return rows


def terminal_observation(messages):
    assistants = [m for m in messages if m.get('info', {}).get('role') == 'assistant']
    last = assistants[-1] if assistants else {}
    info = last.get('info', {})
    finish = info.get('finish')
    reason = 'error' if info.get('error') else 'done' if finish == 'stop' else finish or 'unknown'
    return {'type': 'task_done', 'stop_reason': reason, 'native_finish': finish,
            'native_error': info.get('error'), 'native_message_id': info.get('id'),
            'summary': '\n'.join(p.get('text', '') for p in last.get('parts', []) if p.get('type') == 'text')}


def native_turn_is_terminal(messages):
    assistants = [m for m in messages if m.get('info', {}).get('role') == 'assistant']
    if not assistants:
        return False
    info = assistants[-1]['info']
    return bool(info.get('error') or (info.get('time', {}).get('completed')
                                    and info.get('finish') not in {None, 'tool-calls'}))


def sidecar_host(install, directory, port):
    sidecar = (Path(install) / 'resources/app.asar/out/main/sidecar.js').as_uri()
    command = {'type': 'start', 'hostname': '127.0.0.1', 'port': port,
               'password': 'local-benchmark', 'userDataPath': str(directory / 'state')}
    target = directory / 'sidecar-host.mjs'
    target.write_text("import {EventEmitter} from 'node:events';\n"
        "const p=new EventEmitter(); p.postMessage=(v)=>console.log(JSON.stringify(v)); process.parentPort=p;\n"
        f"await import({json.dumps(sidecar)});\np.emit('message',{{data:{json.dumps(command)}}});\n"
        "process.stdin.on('data',()=>p.emit('message',{data:{type:'stop'}}));\n", encoding='utf-8')
    return target


def local_api_url(base, path, project):
    return base + path + ('' if path.startswith('/global/') else '?' + urlencode({'directory': str(project)}))


def sidecar_ready(log):
    if not log.exists():
        return False
    for line in log.read_text(encoding='utf-8').splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get('type') == 'error':
            raise RuntimeError(f'OpenCode startup: {item.get("error")}')
        if item.get('type') == 'ready':
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('task')
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = local_config(os.environ['LOCAL_OPENAI_BASE_URL'].removesuffix('/v1'),
        os.environ['LOCAL_OPENAI_MODEL'], int(os.environ['AGENT_AB_SEED']),
        float(os.environ['AGENT_AB_TEMPERATURE']), int(os.environ['LOCAL_OPENAI_CONTEXT_RESERVE']),
        int(os.environ['LOCAL_OPENAI_CONTEXT_WINDOW']))
    env = {k: v for k, v in os.environ.items() if not k.endswith(('_API_KEY', '_AUTH_TOKEN', '_OAUTH_TOKEN'))}
    env.update({'ELECTRON_RUN_AS_NODE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
                'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_PROJECT_CONFIG': '1',
                'OPENCODE_CONFIG_CONTENT': json.dumps(config)})
    for key, folder in [('XDG_STATE_HOME', 'state'), ('XDG_DATA_HOME', 'data'),
                        ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache')]:
        env[key] = str(directory / folder)
    for folder in ('state', 'data', 'config', 'cache'):
        (directory / folder).mkdir(exist_ok=True)
    host = sidecar_host(args.install.resolve(), directory, port)
    def api(path, method='GET', data=None, timeout=10):
        request = Request(local_api_url(f'http://127.0.0.1:{port}', path, args.project.resolve()),
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Basic b3BlbmNvZGU6bG9jYWwtYmVuY2htYXJr', 'Content-Type': 'application/json'},
            method=method)
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def emit(row):
        print(json.dumps(row, ensure_ascii=False), flush=True)

    with (directory / 'sidecar.stdout.log').open('ab') as out, (directory / 'sidecar.stderr.log').open('ab') as err:
        proc = subprocess.Popen([str(args.install / 'OpenCode.exe'), str(host)], cwd=args.project,
            env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            started = time.monotonic()
            while not sidecar_ready(directory / 'sidecar.stdout.log'):
                if proc.poll() is not None or time.monotonic() - started > 30:
                    raise RuntimeError('OpenCode sidecar did not become ready')
                time.sleep(.1)
            while True:
                if proc.poll() is not None:
                    raise RuntimeError('OpenCode sidecar exited before health check')
                try:
                    health = api('/global/health', timeout=2)
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() - started > 30:
                        raise
                    time.sleep(.1)
            (directory / 'health.json').write_text(json.dumps(health), encoding='utf-8')
            session_file = directory / 'session.json'
            if session_file.exists():
                session = json.loads(session_file.read_text(encoding='utf-8'))
            else:
                session = api('/session', 'POST', {})
                session_file.write_text(json.dumps(session), encoding='utf-8')
            identity = session['id']
            prefix = '/session/' + identity
            prior = api(prefix + '/message')
            prior_ids = {m['info']['id'] for m in prior}
            api(prefix + '/prompt_async', 'POST', {'model': {'providerID': 'kobold', 'modelID': os.environ['LOCAL_OPENAI_MODEL']},
                'agent': 'build', 'parts': [{'type': 'text', 'text': args.task}]})
            seen = set()
            aborted = False
            while True:
                messages = [m for m in api(prefix + '/message') if m['info']['id'] not in prior_ids]
                for row in observe_messages(messages, seen):
                    emit(row)
                statuses = api('/session/status')
                if not aborted and (Path(os.environ['AGENT_AB_TRACE']) / 'stop').exists():
                    api(prefix + '/abort', 'POST')
                    aborted = True
                if (aborted or native_turn_is_terminal(messages)) and statuses.get(identity, {}).get('type', 'idle') == 'idle':
                    if aborted:
                        emit({'type': 'task_done', 'stop_reason': 'stopped', 'native_abort_acknowledged': True})
                    else:
                        emit(terminal_observation(messages))
                    (directory / 'messages.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding='utf-8')
                    break
                permissions = api('/permission')
                pending = [p for p in permissions if p.get('sessionID') == identity]
                if pending:
                    emit({'type': 'permission_pending', 'requests': pending})
                    api(prefix + '/abort', 'POST')
                    emit({'type': 'task_done', 'stop_reason': 'permission_pending'})
                    return 3
                time.sleep(.1)
            return 0
        finally:
            if proc.poll() is None:
                if proc.stdin:
                    proc.stdin.write(b'stop\n')
                    proc.stdin.flush()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=5)


if __name__ == '__main__':
    try:
        result = main()
    except Exception as exc:
        print(json.dumps({'type': 'observer_error', 'error': f'{type(exc).__name__}: {exc}'}), flush=True)
        raise
    raise SystemExit(result)
