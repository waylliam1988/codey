"""Frozen-arm calibration changes semantic pressure, leaving receipt batching intact."""
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from tests.manual import context_compaction_benchmark_ab as benchmark


def test_semantic_ratio_calibration_does_not_change_receipt_batching_or_the_checkout(tmp_path, monkeypatch):
    original = (Path(__file__).parents[1] / 'codey/providers/api_provider.py').read_text(encoding='utf-8')
    frozen = []

    def run(command, **kwargs):
        if command[0] == 'git':
            archive = next(value.removeprefix('--output=') for value in command if value.startswith('--output='))
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr('codey/__init__.py', '')
        else:
            config = json.loads(Path(command[-1]).read_text(encoding='utf-8'))
            if config['arm'] == 'after':
                frozen.append((Path(config['root']) / 'codey/providers/api_provider.py').read_text(encoding='utf-8'))
            Path(config['output']).write_text(json.dumps({'case': config['case'], 'seed': config['seed'],
                'arm': config['arm'], 'success': True, 'total_tokens': 1}), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(benchmark.subprocess, 'run', run)
    monkeypatch.setattr(benchmark.subprocess, 'check_output', lambda *args, **kwargs: 'fixture')
    monkeypatch.setattr('sys.argv', ['benchmark', '--cases', 'correction', '--repeats', '1',
        '--maintenance-ratio', '0.9', '--output', str(tmp_path / 'report.json')])
    assert benchmark.main() == 0
    assert 'pressure >= self.context_budget.input_limit * 0.90' in frozen[0]
    assert 'pressure >= self.context_budget.input_limit * 0.60' in frozen[0]
    assert (Path(__file__).parents[1] / 'codey/providers/api_provider.py').read_text(encoding='utf-8') == original
