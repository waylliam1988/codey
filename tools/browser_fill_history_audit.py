"""Audit saved fill entrances, frozen originals and actual values without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

from codey.operations.browser.browser_followups import explicit_fill_text
from codey.operations.kernel.kernel_protocol import _response_objects
from codey.protocols.text_turn import decode_text_turn
from codey.providers.response_record import decode_reply
from codey.runtime.core.models import ToolResult
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_integer,
)
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import production_source_digest, write_json


def frozen_original(snapshot: JsonObject) -> str | None:
    text = snapshot.get('browser_fill_text')
    if isinstance(text, str):
        return text
    # The historical experiment serialized its binding under this exact key.
    experiment = snapshot.get('experiment_fill_binding')
    if isinstance(experiment, dict) and experiment.get('source') == 'task_text':
        original = experiment.get('text')
        return original if isinstance(original, str) else None
    return None


def classify(task: str, snapshot: JsonObject, response: JsonObject,
             fills: list[ToolResult]) -> JsonObject:
    reply = decode_reply(response)
    items = _response_objects(decode_text_turn(reply) if isinstance(reply, str) else reply)
    names = [name for name, _, _ in items] if isinstance(items, list) else []
    entrance = 'direct' if 'browser_fill' in names else 'nested' if 'codemode' in names else 'other'
    captured = frozen_original(snapshot)
    expected = captured if captured is not None else explicit_fill_text(task)
    successful = [row for row in fills if row.ok]
    mismatched = [row for row in successful if expected is not None and row.call.args.get('value') != expected]
    source_selected = any(name == 'browser_fill' and args.get('source') == 'task_text'
                          for name, args, _ in items) if isinstance(items, list) else False
    return {'entrance': entrance, 'eligible_under_current_parser': explicit_fill_text(task) is not None,
        'original_frozen_in_that_request': captured is not None, 'direct_source_selected': source_selected,
        'executed_fills': len(fills), 'successful_fills': len(successful),
        'values_comparable': expected is not None, 'mismatched_fills': len(mismatched),
        'nested_mismatch_with_frozen_original': entrance == 'nested' and captured is not None and bool(mismatched),
        'actual_values': [row.call.args.get('value') for row in successful],
        'expected_original': expected}


def task_text(directory: Path) -> str:
    task = directory / 'task.txt'
    if task.exists():
        return task.read_text(encoding='utf-8')
    data = parse_json_object((directory / 'input.json').read_text(encoding='utf-8'))
    text = data.get('task')
    if not isinstance(text, str):
        raise ValueError('Saved input has no original task text.')
    return text


def audit_directory(directory: Path) -> list[JsonObject]:
    task = task_text(directory)
    saved = artifacts(directory / 'state')
    requests = {str(row['effect_id']): decode_json_object(row['payload']) for _, row in saved
                if row.get('kind') == 'provider_request'}
    responses = {str(row['effect_id']): decode_json_object(row['payload']) for _, row in saved
                 if row.get('kind') == 'provider_response'}
    receipts = {row.identity: row.result for row in tool_receipts(directory / 'state')}
    by_turn: dict[int, list[ToolResult]] = {}
    with sqlite3.connect((directory / 'state/durable.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as db:
        stored: tuple[object, ...]
        for stored in db.execute("SELECT intent FROM effects WHERE category='tool_call' ORDER BY rowid"):
            raw: object = stored[0]
            if not isinstance(raw, str):
                raise ValueError('Invalid stored tool intent.')
            intent = parse_json_object(raw)
            result = receipts.get(str(intent['effect_id']))
            if result is not None and result.call.name == 'browser_fill':
                by_turn.setdefault(require_json_integer(intent['turn'], 'turn'), []).append(result)
    rows: list[JsonObject] = []
    seen: set[int] = set()
    for identity, request in requests.items():
        turn = require_json_integer(request['logical_turn'], 'logical_turn')
        fills = by_turn.get(turn, [])
        if not fills or identity not in responses:
            continue
        if turn in seen:
            raise ValueError('Multiple saved requests own a fill turn; attribution is ambiguous.')
        seen.add(turn)
        row = classify(task, decode_json_object(request['snapshot']), responses[identity], fills)
        row.update(directory=str(directory), turn=turn, provider_effect_id=identity,
                   task_sha256=hashlib.sha256(task.encode('utf-8')).hexdigest())
        rows.append(row)
    if seen != set(by_turn):
        raise ValueError('An executed fill has no uniquely attributable original provider request.')
    return rows


def audit_roots(roots: list[Path]) -> JsonObject:
    directories = sorted({path.parent for root in roots for path in root.rglob('provider.jsonl')})
    if not directories:
        raise ValueError('No saved provider records were found; an empty audit is not evidence.')
    rows: list[JsonObject] = []
    errors: list[JsonObject] = []
    for directory in directories:
        try:
            rows.extend(audit_directory(directory))
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            errors.append({'directory': str(directory), 'error': f'{type(exc).__name__}: {exc}'})
    totals = Counter(str(row['entrance']) for row in rows)
    return canonical_json_object({'roots': [str(root) for root in roots], 'directories': len(directories),
        'audit_production_digest': production_source_digest(),
        'audit_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'audited_fill_turns': len(rows), 'entrances': dict(totals),
        'nested_mismatch_with_frozen_original': sum(row['nested_mismatch_with_frozen_original'] is True for row in rows),
        'current_parser_eligible_nested_mismatch': sum(row['entrance'] == 'nested'
            and row['eligible_under_current_parser'] is True and row['mismatched_fills'] != 0 for row in rows),
        'unreadable_directories': errors, 'complete': not errors, 'rows': rows})


class Options(argparse.Namespace):
    roots: list[Path]
    output: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots', nargs='+', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(namespace=Options())
    if any(not root.is_dir() for root in args.roots):
        parser.error('Every history root must exist.')
    if args.output.exists():
        parser.error('Use a new audit output; original results must remain intact.')
    result = audit_roots(args.roots)
    write_json(args.output, result)
    status: JsonObject = {key: result[key] for key in result if key != 'rows'}
    print(json.dumps(status, ensure_ascii=False))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
