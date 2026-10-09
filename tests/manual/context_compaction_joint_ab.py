"""Run old Codey, OpenCode and Pi paired comparisons serially, then aggregate.

python -m tests.manual.context_compaction_joint_ab --repeats 3
Only the selected loopback model is used. No remote provider credentials.
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.context_compaction_benchmark.matrix import comparison_matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--before-ref', default='a28df7be')
    parser.add_argument('--keep-recent-tokens', type=int, default=2000)
    parser.add_argument('--cases', help='Same replay cases for all three comparisons')
    parser.add_argument('--maintenance-ratio', type=float, choices=(0.6, 0.8, 0.9))
    parser.add_argument('--output-dir', type=Path, default=Path('artifacts/context-compaction-ab/joint'))
    parser.add_argument('--aggregate-only', action='store_true')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reports = {}
    modules = {'old-codey':'context_compaction_benchmark_ab', 'opencode':'codey_vs_opencode_compaction_ab',
               'pi':'codey_vs_pi_compaction_ab'}
    with tempfile.TemporaryDirectory(prefix='codey-joint-snapshot-') as directory:
        snapshot = Path(directory)
        if not args.aggregate_only:
            shutil.copytree(Path(__file__).resolve().parents[2] / 'codey', snapshot / 'codey',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name, module in modules.items():
            output = args.output_dir / f'{name}.json'
            if not args.aggregate_only:
                extra = (['--cases', args.cases] if args.cases else [])
                if args.maintenance_ratio is not None:
                    extra += ['--maintenance-ratio', str(args.maintenance_ratio)]
                subprocess.run([sys.executable, '-m', f'tests.manual.{module}', '--repeats', str(args.repeats),
                                '--after-root', str(snapshot), '--keep-recent-tokens', str(args.keep_recent_tokens),
                                '--before-ref', args.before_ref,
                                '--output', str(output), *extra], check=True)
            reports[name] = json.loads(output.read_text(encoding='utf-8'))
    result = comparison_matrix(reports)
    (args.output_dir / 'matrix.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
