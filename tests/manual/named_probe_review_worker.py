"""Signature/protocol ablation and shared-proposal reviewer child only."""
import argparse
import difflib
import json
from pathlib import Path

from tests.manual import counterexample_review_experiment_worker as base
from tests.manual.named_probe_review_adapter import NamedProbeReviewer, generate_plan


def planning(spec, args):
    import subprocess
    import sys

    from codey.reviews.core import render_review_prompt
    from tests.manual.agent_stability_codey_worker import sampling_provider
    source = spec['candidate']
    checked = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-v'], cwd=args.project,
                             capture_output=True, text=True, timeout=15, check=False)
    if checked.returncode:
        raise ValueError('frozen candidate fails visible setup tests')
    context = render_review_prompt(project='frozen-experiment-fixture', task=spec['task'],
        writer_summary='Frozen candidate submitted after visible verification.',
        changes={'files': [{'path': 'app.py', 'status': 'M'}], 'diff': ''.join(difflib.unified_diff(
            spec['files']['app.py'].splitlines(True), source.splitlines(True), fromfile='a/app.py', tofile='b/app.py'))},
        project_map='Current app.py:\n'+source, execution_evidence='Actual visible verification:\n'+checked.stderr[-2000:])
    delegate = sampling_provider('local')
    try:
        row = generate_plan(delegate, args.project, spec['task'], context, native=args.arm!='plan-text', timeout=90)
    finally:
        delegate.close()
    # Observe AFTER the model plan exits; never feed reference or observed values to planning.
    from tests.manual.counterexample_review_adapter import observe
    probes = [h['probe'] for h in row['hypotheses']]
    row['actual'] = observe(args.project, probes, authorized=True) if probes else None
    (args.state_home/'counterexamples.jsonl').write_text(json.dumps(row)+'\n', encoding='utf-8')
    (args.state_home/'discovery.json').write_text(json.dumps({'status':'plan_only','verdict':None,
        'recorded_plan': {k:v for k,v in row.items() if k!='actual'}}), encoding='utf-8')
    return 0


def main():
    parser = argparse.ArgumentParser()
    for key in ('project','state-home','manifest'):
        parser.add_argument('--'+key, type=Path, required=True)
    parser.add_argument('--phase', choices=('plan','discovery','repair'), required=True)
    parser.add_argument('--arm', choices=('plan-text','plan-native','codey','native-data','native-observed','no-review'),
                        required=True)
    parser.add_argument('--max-turns',type=int,default=12)
    args = parser.parse_args()
    spec = json.loads(args.manifest.read_text(encoding='utf-8'))
    if set(spec)-{'task','files','candidate','prepared_plan'} or not {'task','files','candidate'} <= set(spec):
        raise ValueError('manifest contains unintended offline data')
    args.state_home.mkdir(parents=True,exist_ok=True)
    if args.phase=='plan':
        return planning(spec,args)
    if args.arm in {'native-data','native-observed'}:
        execute = args.arm=='native-observed'
        def factory(delegate, project, task, log, **kw):
            return NamedProbeReviewer(delegate,project,task,log,execute=execute,prepared_plan=spec.get('prepared_plan'))
        base.CounterexampleReviewer = factory
        args.arm='counterexample'
    return base.discovery(spec,args) if args.phase=='discovery' else base.repair(spec,args)


if __name__=='__main__':
    raise SystemExit(main())
