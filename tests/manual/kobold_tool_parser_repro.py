"""Isolate KoboldCpp's Python tool parser without Codey or model generation.

Extracts audited functions unchanged from the installed source; does not patch
the installation or change server settings. The parent bounds a child process.
"""
from __future__ import annotations

import argparse
import ast
import faulthandler
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

FUNCTIONS = {'fix_unquoted_keys', 'toolcall_to_normalized_json'}


def worker(source, value):
    tree = ast.parse(source.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    assert len(tree.body) == len(FUNCTIONS)
    namespace = {'json': json, 're': re, 'cached_chat_template': value['template']}
    exec(compile(tree, str(source), 'exec'), namespace)
    faulthandler.dump_traceback_later(1)
    print('fallback_parser_entered', flush=True)
    text = value['text'].removeprefix('<|tool_call>').removesuffix('<tool_call|>')
    result = namespace['toolcall_to_normalized_json'](text, '<|tool_call>', '<tool_call|>', None)
    print(json.dumps({'result': result}), flush=True)
    faulthandler.cancel_dump_traceback_later()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--stall', type=Path)
    parser.add_argument('--props', type=Path)
    parser.add_argument('--requests', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    if args.worker:
        worker(args.source.resolve(), json.load(sys.stdin))
        return
    text = json.loads(args.stall.read_text(encoding='utf-8'))['/api/extra/generate/check']['results'][0]['text']
    template = json.loads(args.props.read_text(encoding='utf-8'))['chat_template']
    batch = json.loads(args.requests.read_text(encoding='utf-8'))
    tools = next(request['tools'] for row in batch['rows'] for request in row['requests']
                 if any(t['function']['name'] == 'edit' for t in request.get('tools', [])))
    corrected = text.replace('<|"|>},path:', '<|"|>}],path:')
    assert corrected != text
    rows = []
    for name, content in [('balanced_control', corrected), ('observed_malformed', text)]:
        row = {'case': name, 'text': content, 'tools': tools, 'template': template}
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--source', str(args.source), '--worker']
        try:
            result = subprocess.run(command, input=json.dumps(row), capture_output=True, text=True, timeout=3)
            row.update(timed_out=False, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired as exc:
            row.update(timed_out=True, exit_code=None,
                       stdout=exc.stdout.decode(errors='replace') if isinstance(exc.stdout, bytes) else exc.stdout or '',
                       stderr=exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr or '')
        rows.append(row)
        print(json.dumps({k: row[k] for k in ('case', 'timed_out', 'exit_code', 'stdout', 'stderr')}), flush=True)
    source = args.source.read_text(encoding='utf-8')
    args.output.write_text(json.dumps({'source': str(args.source),
        'source_sha256': hashlib.sha256(args.source.read_bytes()).hexdigest(),
        'function_sources': {node.name: ast.get_source_segment(source, node) for node in ast.parse(source).body
                             if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'no_codey_imports': True, 'no_model_loaded_in_child': True, 'timeout_seconds': 3,
        'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
