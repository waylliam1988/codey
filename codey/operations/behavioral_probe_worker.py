"""Fixed synchronous JSON-call worker. Input is data, never a generated script.

The copied Python surface protects the source worktree, not the host OS.
Importing project code is still an authorized project verification execution.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def probe(root: Path) -> dict[str, object]:
    manifest = json.loads((root / 'probe.json').read_text(encoding='utf-8'))
    source = (root / manifest['path']).resolve()
    source.relative_to(root.resolve())
    sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location(source.stem, source)
        if spec is None or spec.loader is None:
            raise ValueError('binding_invalid')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        target = getattr(module, manifest['function'])
    except ModuleNotFoundError as exc:
        return {'status': 'not_run', 'reason': 'dependency_missing', 'summary': str(exc), 'rows': []}
    except Exception as exc:
        return {'status': 'not_run', 'reason': 'import_failed', 'summary': type(exc).__name__ + ': ' + str(exc), 'rows': []}
    rows: list[dict[str, object]] = []
    for left, right in manifest['pairs']:
        try:
            a, b = target(left), target(right)
        except Exception as exc:
            return {'status': 'fail', 'reason': 'target_exception', 'summary': type(exc).__name__ + ': ' + str(exc), 'rows': rows}
        try:
            a_json, b_json = (json.dumps(value, allow_nan=False, sort_keys=True) for value in (a, b))
        except (TypeError, ValueError) as exc:
            return {'status': 'not_run', 'reason': 'unsupported_return', 'summary': str(exc), 'rows': rows}
        rows.append({'left': left, 'right': right, 'left_value': a, 'right_value': b, 'equal': a_json == b_json})
    mismatches = [row for row in rows if not row['equal']]
    summary = json.dumps(mismatches[:1] or rows[:1], ensure_ascii=False)
    return {'status': 'fail' if mismatches else 'pass', 'reason': 'property_mismatch' if mismatches else 'matched',
            'summary': summary, 'rows': rows}


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    result = probe(root)
    (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
