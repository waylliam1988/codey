"""Process-local reviewer substitution; production lifecycle and files stay unchanged."""
import argparse
import json
from pathlib import Path

from tests.manual import counterexample_review_experiment_worker as base
from tests.manual.fact_only_review_adapter import FactOnlyReviewer
from tests.manual.named_probe_review_adapter import NamedProbeReviewer
from tests.manual.named_probe_review_worker import planning


def main():
    parser=argparse.ArgumentParser()
    for key in ('project','state-home','manifest'):
        parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--phase',choices=('plan','discovery','repair'),required=True)
    parser.add_argument('--arm',choices=('plan-native','codey','exposed','fact-only'),required=True)
    parser.add_argument('--max-turns',type=int,default=24)
    args=parser.parse_args()
    spec=json.loads(args.manifest.read_text(encoding='utf-8'))
    if set(spec)-{'task','files','candidate','prepared_plan'} or not {'task','files','candidate'}<=set(spec):
        raise ValueError('manifest contains unintended offline data')
    args.state_home.mkdir(parents=True,exist_ok=True)
    if args.phase=='plan':
        return planning(spec,args)
    if args.arm in ('exposed','fact-only'):
        reviewer=FactOnlyReviewer if args.arm=='fact-only' else NamedProbeReviewer
        def factory(delegate,project,task,log,**kw):
            return reviewer(delegate,project,task,log,execute=True,prepared_plan=spec.get('prepared_plan'))
        base.CounterexampleReviewer=factory
        args.arm='counterexample'
    return base.discovery(spec,args) if args.phase=='discovery' else base.repair(spec,args)


if __name__=='__main__':
    raise SystemExit(main())
