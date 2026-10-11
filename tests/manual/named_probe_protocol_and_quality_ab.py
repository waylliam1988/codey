"""Native vs text protocol, then shared-plan observation ablation on new tasks.

No production edits. Three preregistered stages and no outcome-based selection.
python -m tests.manual.named_probe_protocol_and_quality_ab --run-dir artifacts/NAME
"""
import argparse
import hashlib
import json
import subprocess
import threading
from pathlib import Path
from urllib.request import urlopen

from tests.manual.agent_stability_proxy import Proxy, local_url
from tests.manual.counterexample_review_cases import cases as old_cases
from tests.manual.counterexample_review_quality_ab import ROOT, run_arm
from tests.manual.named_probe_review_cases import cases, score_probe

WORKER = 'tests.manual.named_probe_review_worker'


def summary(rows):
    result = {}
    for stage in ('protocol','proposal','semantic','repair'):
        result[stage] = {}
        for arm in sorted({r['arm'] for r in rows if r['stage']==stage}):
            selected = [r for r in rows if r['stage']==stage and r['arm']==arm]
            bugs = [r for r in selected if r['buggy']]
            controls = [r for r in selected if not r['buggy']]
            evidence = [e for r in selected for e in r['evidence']]
            probes = [p for r in selected for p in r['offline_probe_scores']]
            result[stage][arm] = {
                'runs':len(selected),'bug_cases':len(bugs),'correct_controls':len(controls),
                'protocol_usable':sum(e.get('protocol_usable') is True for e in evidence),
                'invalid_proposals':sum(bool(e.get('errors')) for e in evidence),
                'probes':len(probes),'valid_domain_probes':sum(p['valid_domain'] is True for p in probes),
                'valid_counterexamples':sum(p['expected_correct'] is True and p['distinguishes'] for p in probes),
                'wrong_expectations':sum(p['expected_correct'] is False for p in probes),
                'bug_rejections':sum(r['first_review']['verdict']=='changes_requested' for r in bugs),
                'false_rejections':sum(r['first_review']['verdict']=='changes_requested' for r in controls),
                'unavailable_reviews':sum(r['first_review']['status']!='available' for r in selected),
                'qualified_patches':sum(r['qualified_patch'] for r in selected),
                'full_successes':sum(r['success'] for r in selected),
                'bug_full_successes':sum(r['success'] for r in bugs),
                'correct_full_successes':sum(r['success'] for r in controls),
                'false_completions':sum(r['false_completion'] for r in selected),
                'correct_regressions':sum(not r['verification']['hidden_passed'] for r in controls),
                'known_tokens':sum(r['usage']['known_token_usage'] or 0 for r in selected),
                'seconds':sum(r['seconds'] for r in selected),
                'harness_errors':sum(r['harness_error'] for r in selected)}
    return result


def script_hashes():
    names = ['counterexample_observation_worker.py','counterexample_review_adapter.py',
             'counterexample_review_cases.py','counterexample_review_quality_ab.py',
             'counterexample_review_experiment_worker.py','open_code_review_self_review_ab.py',
             'named_probe_review_adapter.py','named_probe_review_cases.py','named_probe_review_worker.py',
             'named_probe_protocol_and_quality_ab.py']
    return {name: hashlib.sha256((ROOT/'tests/manual'/name).read_bytes()).hexdigest() for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--upstream',default='http://127.0.0.1:5001')
    parser.add_argument('--seeds',nargs='+',type=int,default=[81,82])
    parser.add_argument('--timeout',type=int,default=240)
    parser.add_argument('--max-turns',type=int,default=12)
    parser.add_argument('--max-tokens',type=int,default=1536)
    parser.add_argument('--request-limit',type=int,default=24)
    parser.add_argument('--temperature',type=float,default=.2)
    options = parser.parse_args()
    options.upstream = local_url(options.upstream)
    options.run_dir = options.run_dir.resolve()
    options.run_dir.mkdir(parents=True,exist_ok=False)
    with urlopen(options.upstream+'/v1/models',timeout=5) as response:
        model = json.load(response)['data'][0]['id']
    from codey.task.entry_auth import derive_entry_auth
    selected = cases()
    for case in (*old_cases(),*selected):
        if 'project.write' in derive_entry_auth({'task':case.task,'intent':'project'},project='fixture').denied_capabilities:
            raise ValueError('fixture does not authorize edit')
    production = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codey').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    frozen_scripts = script_hashes()
    plan = {'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'model':model,'seeds':options.seeds,'controls':vars(options)|{'run_dir':str(options.run_dir)},
        'production_hashes':production,'script_hashes':frozen_scripts,
        'stages':{'protocol':16,'proposal':8,'semantic':24,'repair':24},
        'cases':[{'name':c.name,'task':c.task,'candidate_sha256':hashlib.sha256(c.candidate.encode()).hexdigest(),
                  'oracle_sha256':hashlib.sha256(c.oracle.encode()).hexdigest()} for c in selected],
        'rules':['Same new-task real model proposal reused in data-only and observed review.',
            'Expected values never upgraded to oracle; reference only offline.',
            'Protocol study changes only transport; same signature/schema/context/prompt body.',
            'New-task native transport chosen in advance regardless of protocol outcome.',
            'Frozen author candidates, not natural Writer self-review.',
            'No production permission or completion bypass. No retuning or valid-run exclusions.']}
    (options.run_dir/'preregistered.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    proxy = Proxy(('127.0.0.1',0),options.upstream,options.timeout,journal=options.run_dir/'http.jsonl',request_limit=options.request_limit)
    thread = threading.Thread(target=proxy.serve_forever,daemon=True)
    thread.start()
    rows, prepared = [], {}
    def save():
        (options.run_dir/'result.json').write_text(json.dumps({'plan':plan,'runs':rows,'summary':summary(rows)},
            ensure_ascii=False,indent=2),encoding='utf-8')
    def execute(case,seed,arm,stage,*,shared=None):
        directory = options.run_dir/f'{stage}-{case.name}-{seed}-{arm}'
        row = run_arm(case,seed,arm,'repair' if stage=='repair' else 'discovery',directory,proxy,options,model,
            worker_module=WORKER,worker_phase='plan' if stage in {'protocol','proposal'} else None,
            extra_manifest={'prepared_plan':shared} if shared else None,
            **({'evidence_scorer':score_probe} if stage!='protocol' else {}))
        row['stage']=stage
        rows.append(row)
        save()
        print(json.dumps({'stage':stage,'case':case.name,'seed':seed,'arm':arm,
            'verdict':row['first_review']['verdict'],'success':row['success'],
            'seconds':round(row['seconds'],2)}),flush=True)
        if row['harness_error']:
            raise RuntimeError('worker did not create an observation; study halted')
        return row
    try:
        # Protocol is tested on old BUG fixtures only; no candidate selection follows.
        for index,case in enumerate(c for c in old_cases() if c.buggy):
            for seed in options.seeds:
                order = ['plan-text','plan-native'] if (index+seed)%2 else ['plan-native','plan-text']
                for arm in order:
                    execute(case,seed,arm,'protocol')
        # Real model proposals are frozen BEFORE either final-review arm sees values.
        for case in selected:
            for seed in options.seeds:
                row=execute(case,seed,'plan-native','proposal')
                prepared[(case.name,seed)] = row['reviews'][0]['review']['recorded_plan']
        for stage in ('semantic','repair'):
            for index,case in enumerate(selected):
                for seed in options.seeds:
                    order = (['codey','native-data','native-observed'] if stage=='semantic'
                             else ['no-review','codey','native-observed'])
                    shift = (index+options.seeds.index(seed))%len(order)
                    for arm in order[shift:]+order[:shift]:
                        execute(case,seed,arm,stage,shared=prepared[(case.name,seed)] if arm.startswith('native-') else None)
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)
        save()
        current = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codey').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        if current!=production or script_hashes()!=frozen_scripts:
            raise RuntimeError('production or frozen experiment scripts changed')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
