"""Isolated loopback-only context replay and real coding tasks. Manual, never CI inference."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CASES = ('repeated', 'correction', 'test-result', 'model-change', 'summary-failure', 'real-task', 'tool-heavy', 'segmented', 'incremental',
         'receipt-recovery', 'unrecoverable-facts')

REFERENCE_SOURCES = {
    'opencode': ('packages/core/src/session/compaction.ts', 'packages/core/src/util/token.ts'),
    'pi': ('packages/ai/src/utils/text.ts', 'packages/coding-agent/src/core/messages.ts',
           'packages/coding-agent/src/core/compaction/utils.ts', 'packages/coding-agent/src/core/compaction/compaction.ts',
           'packages/coding-agent/src/core/session-manager.ts', 'packages/coding-agent/src/core/usage-totals.ts'),
}


def tree_digest(root):
    value = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            value.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes())
    return value.hexdigest()


def freeze_reference(root, destination, name):
    hashes = {}
    for relative in REFERENCE_SOURCES[name]:
        source, target = root / relative, destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    return hashes


def validate_endpoint(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'} or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') != '/v1'):
        raise ValueError('benchmark requires an unambiguous loopback /v1 endpoint')
    return url.rstrip('/')


def verify_import(root, imported):
    if not Path(imported).resolve().is_relative_to(root.resolve()):
        raise ValueError('worker imported the wrong Codey checkout')


def fixture_history(case, output):
    padding = 'Unrelated archived observation: the sample uses a temporary workspace.\n' * 650
    history = [{'role': 'user', 'content': 'Preserve public API.\n' + padding + '\nBinding constraint: no-db. Target: old.py.'},
        {'role': 'assistant', 'content': 'I will follow that constraint.'},
        {'role': 'user', 'content': 'Correction: target is app.py, not old.py. Keep constraint no-db.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'pytest-call', 'type': 'function', 'function': {
            'name': 'run', 'arguments': '{"command":"python -m pytest -q","path":"."}'}}]},
        {'role': 'tool', 'tool_call_id': 'pytest-call', 'content': 'Command completed. Exit code: 1. Stored result: exec-17\n' + output},
        {'role': 'assistant', 'content': 'Read the existing result before next action.'}]
    if case == 'tool-heavy':
        history[0]['content'] = 'Preserve public API. Binding constraint: no-db. Target: old.py.'
        older = []
        for index in range(8):
            older += [{'role':'assistant','tool_calls':[{'id':f'old-{index}','type':'function',
                'function':{'name':'read','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':f'old-{index}','content':('Archived file observation.\n' * 300)}]
        history[1:1] = older
    elif case == 'test-result':
        history[2:2] = [
            {'role': 'assistant', 'tool_calls': [{'id':'older-pytest','type':'function','function':{
                'name':'run','arguments':'{"command":"python -m pytest -q","path":"."}'}}]},
            {'role':'tool','tool_call_id':'older-pytest','content':'Earlier workspace: Exit code: 0. Stored result: obsolete-16'},
            {'role':'assistant','content':'Files changed after that older verification.'}]
    elif case == 'segmented':
        history[0]['content'] = 'Preserve public API. Binding constraint: no-db. Target: old.py.'
        turns = []
        for index in range(8):
            turns += [{'role':'user','content': f'Review archived module {index}; keep the binding constraint.'},
                      {'role':'assistant','content': '\n'.join(
                          f'Archived review {index}, observation {line}: module_{index}/file_{line}.py uses explicit local state and deterministic validation.'
                          for line in range(55))}]
        history[2:2] = turns
    elif case in {'incremental', 'receipt-recovery', 'unrecoverable-facts'}:
        history[0]['content'] = 'Preserve public API. Binding constraint: no-db. Target: old.py.'
        history[2]['content'] = 'Correction: target is app.py, not old.py.'
    return history


def install_network_boundary(endpoint, failures=None, timings=None):
    expected = urllib.parse.urlsplit(endpoint).netloc
    original = urllib.request.OpenerDirector.open

    def local_open(self, fullurl, *args, **kwargs):
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.netloc != expected:
            raise ValueError('benchmark blocked a non-selected network destination')
        started = time.monotonic() if timings is not None else None
        response = original(self, fullurl, *args, **kwargs)
        if failures is not None or timings is not None:
            read = response.read
            def observe_read(*read_args, **read_kwargs):
                raw = read(*read_args, **read_kwargs)
                if timings is not None:
                    timings.append({'path': parsed.path, 'seconds': round(time.monotonic() - started, 6)})
                if failures is not None:
                    try:
                        json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        failures.append({'path': parsed.path, 'status': response.status, 'bytes': len(raw),
                                         'head': raw[:500].decode('utf-8', 'replace')})
                return raw
            response.read = observe_read
        return response

    urllib.request.OpenerDirector.open = local_open


def real_task(provider, project):
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run
    from codey.workspace.revision import WorkspaceRevisionStore
    events = []
    result = run(AgentRequest(provider=provider, project=project,
        task='Change only RATE in app.py from 5 to 7, then verify with python -m pytest -q. Do not change tests.',
        fresh_chat=False, max_turns=12, on_event=events.append, project_changes_required=True,
        workspace_revision_store=WorkspaceRevisionStore(project / ".codey" / "benchmark-state")))
    check = subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=project, capture_output=True, text=True, timeout=30)
    completed_tools = [event for event in events if event.kind == 'tool' and event.call is not None and event.outcome is not None]
    seen_commands, duplicates, revision = set(), 0, 0
    commands = []
    for event in completed_tools:
        if event.outcome.changed:
            revision += 1
        if event.call.name == 'run' and event.outcome.audit.get('command_started_at'):
            key = (str(event.call.args.get('command')), str(event.call.args.get('path', '.')), revision)
            duplicates += key in seen_commands
            seen_commands.add(key)
            commands.append({'command': key[0], 'cwd': key[1], 'workspace_revision': revision,
                             'exit_code': event.outcome.audit.get('exit_code')})
    return {'success': result.stop_reason == 'done' and check.returncode == 0 and (project / 'test_app.py').read_text(encoding='utf-8') == 'from app import RATE\ndef test_rate():\n    assert RATE == 7\n',
            'kernel_stop_reason': result.stop_reason, 'kernel_summary': result.summary,
            'independent_check': check.stdout, 'event_count': len(events), 'executed_commands': commands,
            'duplicate_executions': duplicates, 'workspace_check_passed': check.returncode == 0}


def worker(config):
    root = Path(config['root']).resolve()
    sys.path.insert(0, str(root))
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    import codey
    from codey.providers import api_transport
    from codey.providers.api_provider import ApiProvider
    from codey.providers.local_tokens import KoboldRequestCounter
    from codey.providers.local_usage import configure_usage, parser_for

    verify_import(root, str(codey.__file__))
    endpoint = validate_endpoint(config['endpoint'])
    network_failures = []
    request_timings = []
    install_network_boundary(endpoint, network_failures, request_timings)
    server = endpoint.removesuffix('/v1')

    def metadata(path):
        with urllib.request.urlopen(server + path, timeout=10) as response:
            return json.loads(response.read(1024 * 1024))

    version = metadata('/api/extra/version')
    if version.get('result') != 'KoboldCpp' or version.get('jinja') is not True:
        raise ValueError('benchmark requires KoboldCpp with Jinja full-request counting')
    model = metadata('/api/v1/model')['result']
    capacity = metadata('/api/extra/true_max_context_length')['value']
    window = config['window']
    if capacity < window:
        raise ValueError('logical window exceeds running server capacity')
    usage = []
    transport_failures = []
    original_parse = api_transport._json_object
    def observed_parse(raw):
        try:
            return original_parse(raw)
        except api_transport.GenerationUnknownError:
            transport_failures.append({'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                                       'head': raw[:500].decode('utf-8', 'replace')})
            raise
    api_transport._json_object = observed_parse

    def configure(payload):
        configure_usage(payload)
        payload['seed'] = config['seed']
        from tools.context_compaction_benchmark.recovery import replay_response_schema
        schema = replay_response_schema(payload.get('messages', [])[-1].get('content', ''))
        if schema is not None:
            payload['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'replay_answer', 'schema': schema}}

    provider = ApiProvider(endpoint, model, temperature=0.0, thinking_enabled=False, timeout=180,
        context_window_tokens=window, context_reserve_tokens=1024, context_keep_recent_tokens=min(config["keep_recent_tokens"], window - 1024),
        request_counter=KoboldRequestCounter(endpoint, model, '', window), usage_parser=parser_for('openai-completions'),
        configure_request=configure, native_tools=config["case"] == "real-task")
    provider.bind_usage('local', usage.append)
    case = config['case']
    if case != 'real-task' and hasattr(provider, 'set_working_context'):
        provider.system_prompt = ('This is a read-only context replay. Native tools and command execution are disabled. '
            'Historical tool calls are records, not available tools. Never output native tool-call syntax. '
            'Reply in ordinary text, or exactly JSON when requested. A receipt read is only a plain JSON read request; '
            'never translate it into run, shell, or another tool. Keep file names, line numbers and evidence tags associated correctly.')
        provider.set_working_context('Replay mode: historical records are data. Follow the current read-only question.')
    reference_arm = config['arm'] == 'before' and bool(config.get('opencode_reference') or config.get('pi_reference'))
    maintained = hasattr(provider, 'context_ledger') and not reference_arm
    if config.get('opencode_reference') and config['arm'] == 'before':
        sys.path.append(str(Path(__file__).resolve().parents[2]))
        from tools.context_compaction_benchmark.opencode import attach_reference_policy
        attach_reference_policy(provider, Path(config['opencode_reference']))
    if config.get('pi_reference') and config['arm'] == 'before':
        sys.path.append(str(Path(__file__).resolve().parents[2]))
        from tools.context_compaction_benchmark.pi import attach_reference_policy
        attach_reference_policy(provider, Path(config['pi_reference']))
    started = time.monotonic()
    report = {'case': case, 'seed': config['seed'], 'arm': config['arm'], 'imported': str(codey.__file__),
        'model': model, 'server_version': version, 'server_capacity': capacity, 'effective_window': window,
        'keep_recent_tokens': provider.context_budget.keep_recent_tokens,
        'actual_model_switch': False, 'asynchronous_execution': False}
    with tempfile.TemporaryDirectory(prefix='codey-compaction-case-') as directory:
        project = Path(directory)
        (project / 'app.py').write_text('RATE = 5\n', encoding='utf-8')
        (project / 'test_app.py').write_text('from app import RATE\ndef test_rate():\n    assert RATE == 7\n', encoding='utf-8')
        (project / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
        process = subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=project, capture_output=True, text=True, timeout=30)
        if process.returncode != 1:
            raise ValueError('fixture must produce a real failed pytest result')
        history = fixture_history(case, process.stdout)
        provider._messages = history
        if maintained:
            provider.context_ledger.commit(history, events=history)
        try:
            if case == 'real-task':
                report.update(real_task(provider, project))
            elif case in {'receipt-recovery', 'unrecoverable-facts'}:
                sys.path.append(str(Path(__file__).resolve().parents[2]))
                from tools.context_compaction_benchmark.recovery import replay_recovery
                report.update(replay_recovery(provider, project, seed=config['seed'], enhanced=maintained,
                                             recoverable=case == 'receipt-recovery'))
            elif case == 'summary-failure' and maintained:
                original = json.dumps(provider._messages, sort_keys=True)
                provider.summarize_context = lambda *_: (_ for _ in ()).throw(RuntimeError('injected summary failure'))
                try:
                    provider.send('Continue the current work.')
                except RuntimeError:
                    report['success'] = json.dumps(provider._messages, sort_keys=True) == original
                else:
                    report['success'] = False
                report['robustness_only'] = True
            else:
                if case == 'model-change':
                    report['switch_scope'] = 'reconnect same loaded model; not cross-model quality'
                    if maintained:
                        provider.maintain_context()
                        provider.wait_for_maintenance(180)
                        portable = provider.export_work_state()
                        provider.new_chat()
                        provider._messages = [{'role': 'assistant', 'content': portable}]
                    else:
                        provider.new_chat()
                question = ('Return exactly one JSON object with four fields based on the existing work: '
                    'constraint (binding storage constraint), target (latest corrected filename), '
                    'exit_code (completed pytest integer code), result_ref (stored result ID). '
                    'Do not run a command. Use null for unknown facts.')
                report['answer'] = provider.send(question)
                if case == 'repeated':
                    provider._messages += [{'role': 'user', 'content': 'Continue.'},
                        {'role': 'assistant', 'content': 'Historical measurement record.\n' * 2500}]
                    report['answer'] = provider.send(question)
                if case == 'incremental':
                    report['waves_completed'] = 0
                    for wave in range(8):
                        if maintained:
                            provider.wait_for_maintenance(180)
                        observations = [{'role':'assistant','tool_calls':[{'id':f'observation-{wave}', 'type':'function',
                            'function':{'name':'read','arguments':json.dumps({'path':f'module_{wave}.py'})}}]},
                            {'role':'tool','tool_call_id':f'observation-{wave}', 'content':'\n'.join(
                                f'Module {wave}, line {line}: separate local state, explicit dependencies, deterministic checks; archived investigation only.'
                                for line in range(36))},
                            {'role':'assistant','content':f'Reviewed module {wave}. No new verification or storage decision.'}]
                        with provider._state_lock:
                            provider._messages.extend(observations)
                            if maintained:
                                provider._history_revision += 1
                                provider.context_ledger.commit(provider._messages, events=observations)
                        report['answer'] = provider.send(question)
                        report['waves_completed'] += 1
                if case == 'summary-failure':
                    report['robustness_only'] = True
                    report['summary_fault_node'] = 'not available in baseline'
            if maintained:
                provider.wait_for_maintenance(180)
                report['maintenance'] = list(provider._compaction.diagnostics)
                report['summary_states'] = [row['text'] for row in provider.context_ledger.checkpoints() if row.get('text')]
        except Exception as exc:
            report['error'] = f'{type(exc).__name__}: {exc}'
            report['success'] = False
            if type(exc).__name__ == 'ContextOverflowError':
                report['failure_kind'] = 'context_overflow'
                report['failure_detail'] = report.pop('error')
        finally:
            provider.close()
    report['usage'] = [row.to_payload() for row in usage]
    report['transport_failures'] = transport_failures
    report['network_failures'] = network_failures
    report['request_timings'] = request_timings
    report['seconds'] = time.monotonic() - started
    known = all(row.usage.input_tokens is not None and row.usage.output_tokens is not None for row in usage)
    report['total_tokens'] = sum(row.usage.input_tokens + row.usage.output_tokens for row in usage) if known else None
    report['summary_requests'] = sum(getattr(row, 'purpose', 'conversation') == 'compaction' for row in usage)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--before-ref', default='a28df7be')
    parser.add_argument('--after-root', type=Path, help='Codey snapshot root supplied by the joint runner')
    parser.add_argument('--maintenance-ratio', type=float, choices=(0.6, 0.8, 0.9),
                        help='Frozen candidate semantic pressure only; receipt batching and runtime settings stay unchanged')
    parser.add_argument('--base-url', default='http://127.0.0.1:5001/v1')
    parser.add_argument('--window-tokens', type=int, default=8192)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--keep-recent-tokens', type=int, default=2000)
    parser.add_argument('--cases', default=','.join(CASES))
    parser.add_argument('--control', action='store_true')
    parser.add_argument('--opencode-reference', type=Path)
    parser.add_argument('--pi-reference', type=Path)
    parser.add_argument('--output', type=Path, default=Path('artifacts/context-compaction-ab/report.json'))
    args = parser.parse_args()
    if args.worker:
        config = json.loads(args.worker.read_text(encoding='utf-8'))
        Path(config['output']).write_text(json.dumps(worker(config), ensure_ascii=False, indent=2), encoding='utf-8')
        return 0
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from tests.manual.ab_harness_common import write_json_atomic
    from tools.context_compaction_benchmark.scorer import score_trial, verdict

    endpoint = validate_endpoint(args.base_url)
    if not 2048 <= args.window_tokens <= 262144 or not 1 <= args.repeats <= 30 or not 1 <= args.keep_recent_tokens < args.window_tokens:
        raise ValueError('invalid benchmark bounds')
    cases = args.cases.split(',')
    reference = args.opencode_reference or args.pi_reference
    if args.opencode_reference and args.pi_reference:
        raise ValueError('select one reference per paired experiment')
    if reference and args.control:
        raise ValueError('A/A control must use the same Codey baseline on both arms')
    if reference and any(case in {'real-task','summary-failure','model-change'} for case in cases):
        raise ValueError('Reference policy comparison supports history replays, not whole-runtime cases')
    if any(case not in CASES for case in cases):
        raise ValueError('unknown case')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'before_ref': subprocess.check_output(['git', 'rev-parse', args.before_ref], cwd=root, text=True).strip(),
        'after': 'frozen working-tree package', 'control': args.control,
        'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'rows': [], 'verdict': 'inconclusive',
        'limits': ['one loaded model', 'seed does not guarantee determinism', 'completed pytest, not async processes']}
    if reference:
        report['comparison_scope'] = 'Actual reference compaction functions; shared Codey transport, not complete product runtime'
        report['reference_name'] = 'OpenCode' if args.opencode_reference else 'Pi'
        report['reference_ref'] = subprocess.check_output(['git','-C',str(reference),'rev-parse','HEAD'],text=True).strip()
    with tempfile.TemporaryDirectory(prefix='codey-compaction-arms-') as directory:
        temp = Path(directory)
        archive, before = temp / 'before.zip', temp / 'before'
        subprocess.run(['git', 'archive', '--format=zip', f'--output={archive}', args.before_ref], cwd=root, check=True)
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(before)
        after = temp / 'after'
        shutil.copytree((args.after_root or root) / 'codey', after / 'codey',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        if args.maintenance_ratio is not None:
            policy = after / 'codey/providers/api_provider.py'
            text, changed = re.subn(r'(?<=pressure >= self.context_budget.input_limit \* )0\.80',
                                   f'{args.maintenance_ratio:.2f}', policy.read_text(encoding='utf-8'), count=1)
            if changed != 1:
                raise ValueError('candidate maintenance policy source boundary changed')
            policy.write_text(text, encoding='utf-8', newline='\n')
            report['candidate_maintenance_ratio'] = args.maintenance_ratio
        report['after_source_sha256'] = tree_digest(after / 'codey')
        runner = temp / 'runner'
        driver = runner / 'tests/manual' / Path(__file__).name
        driver.parent.mkdir(parents=True)
        driver.write_bytes(Path(__file__).read_bytes())
        shutil.copytree(root / 'tools/context_compaction_benchmark', runner / 'tools/context_compaction_benchmark',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        report['adapter_sha256'] = tree_digest(runner / 'tools/context_compaction_benchmark')
        frozen_reference = temp / 'reference'
        if reference:
            report['reference_source_sha256'] = freeze_reference(reference, frozen_reference,
                                                                'opencode' if args.opencode_reference else 'pi')
        for repeat in range(args.repeats):
            for case in cases:
                for arm in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
                    print(f'{case} seed={repeat + 41} arm={arm}', flush=True)
                    target = before if (arm == 'before' and not reference) or args.control else after
                    config = {'root': str(target), 'enhanced': arm == 'after' and not args.control,
                        'opencode_reference': str(frozen_reference) if args.opencode_reference else None,
                        'pi_reference': str(frozen_reference) if args.pi_reference else None,
                        'arm': arm, 'case': case, 'seed': repeat + 41, 'endpoint': endpoint,
                        'window': args.window_tokens, 'keep_recent_tokens': args.keep_recent_tokens, 'output': str(temp / 'row.json')}
                    config_path = temp / 'config.json'
                    config_path.write_text(json.dumps(config), encoding='utf-8')
                    process = subprocess.run([sys.executable, '-I', str(driver), '--worker', str(config_path)],
                        cwd=temp, capture_output=True, text=True, timeout=1200)
                    if process.returncode:
                        row = {'arm': arm, 'case': case, 'seed': repeat + 41, 'error': process.stderr[-3000:], 'success': False}
                    else:
                        row = json.loads((temp / 'row.json').read_text(encoding='utf-8'))
                        if 'answer' in row:
                            row = score_trial(row, row.get('expected', {'constraint': 'no-db', 'target': 'app.py', 'exit_code': 1, 'result_ref': 'exec-17'}))
                    report['rows'].append(row)
                    write_json_atomic(args.output, report)
    comparable = [row for row in report['rows'] if row['case'] != 'summary-failure']
    report['verdict'] = verdict([row for row in comparable if row['arm'] == 'before'], [row for row in comparable if row['arm'] == 'after'])
    write_json_atomic(args.output, report)
    print(report['verdict'], flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
