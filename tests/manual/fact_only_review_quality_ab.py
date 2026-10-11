"""Preregistered exposed-vs-isolated observations, targeted replay and fresh holdouts.

python -m tests.manual.fact_only_review_quality_ab --run-dir artifacts/NAME
No production edits or outcome-driven reruns; unknown generations halt the study.
"""
import argparse
import hashlib
import json
import subprocess
import threading
from pathlib import Path
from urllib.request import urlopen

from tests.manual.agent_stability_measurements import usage_totals
from tests.manual.agent_stability_proxy import Proxy, local_url
from tests.manual.counterexample_review_quality_ab import ROOT, run_arm
from tests.manual.fact_only_review_holdout_cases import cases, score_probe
from tests.manual.named_probe_resume_quality_ab import production_hashes
from tests.manual.named_probe_review_cases import cases as replay_cases
from tests.manual.named_probe_review_cases import score_probe as replay_score
from tests.manual.open_code_review_self_review_ab import digest, json_lines

WORKER='tests.manual.fact_only_review_worker'
ARMS=('codey','exposed','fact-only')


def eligible_replays(prior):
    plans={(r['case'],r['seed']):r['reviews'][0]['review']['recorded_plan']
           for r in prior['runs'] if r['stage']=='proposal' and r['seconds'] is not None
           and r['reviews'][0]['review']['recorded_plan'].get('protocol_usable')}
    result=[]
    for case in replay_cases():
        for seed in prior['plan']['seeds']:
            if (case.name,seed) in plans:
                result.append((case,seed,plans[(case.name,seed)]))
    return result


def ordered_arms(index):
    shift=index%len(ARMS)
    return ARMS[shift:]+ARMS[:shift]


def summary(rows):
    result={}
    for stage in ('replay','proposal','semantic','repair'):
        result[stage]={}
        for arm in sorted({r['arm'] for r in rows if r['stage']==stage}):
            selected=[r for r in rows if r['stage']==stage and r['arm']==arm]
            complete=[r for r in selected if r.get('seconds') is not None]
            bugs=[r for r in complete if r['buggy']]
            controls=[r for r in complete if not r['buggy']]
            scores=[p for r in complete for p in r['offline_probe_scores']]
            result[stage][arm]={'rows':len(selected),'incomplete_arms':len(selected)-len(complete),
                'bug_rows':len(bugs),'control_rows':len(controls),
                'bug_rejections':sum(r['first_review']['verdict']=='changes_requested' for r in bugs),
                'false_rejections':sum(r['first_review']['verdict']=='changes_requested' for r in controls),
                'unavailable_reviews':sum(r['first_review']['status']!='available' for r in complete) if stage!='proposal' else None,
                'full_successes':sum(r['success'] is True for r in complete) if stage=='repair' else None,
                'bug_full_successes':sum(r['success'] is True for r in bugs) if stage=='repair' else None,
                'control_full_successes':sum(r['success'] is True for r in controls) if stage=='repair' else None,
                'qualified_patches':sum(r['qualified_patch'] is True for r in complete) if stage=='repair' else None,
                'false_completions':sum(r['false_completion'] is True for r in complete) if stage=='repair' else None,
                'domain_valid_probes':sum(p['valid_domain'] is True for p in scores),
                'valid_counterexamples':sum(p['expected_correct'] is True and p['distinguishes'] for p in scores),
                'wrong_expectations':sum(p['expected_correct'] is False for p in scores),
                'known_tokens':sum(r['usage']['known_token_usage'] or 0 for r in selected),
                'usage_complete':all(r['usage']['usage_complete'] for r in selected),
                'known_seconds':sum(r['seconds'] or 0 for r in selected),
                'time_complete':len(selected)==len(complete)}
    return result


def frozen_hashes():
    names=('fact_only_review_adapter.py','fact_only_review_holdout_cases.py','fact_only_review_worker.py',
           'fact_only_review_quality_ab.py','named_probe_review_adapter.py','named_probe_review_worker.py',
           'counterexample_review_adapter.py','counterexample_observation_worker.py',
           'counterexample_review_experiment_worker.py','counterexample_review_quality_ab.py',
           'named_probe_review_cases.py','open_code_review_self_review_ab.py',
           'codey_vs_pi_agent_stability_ab.py','agent_stability_codey_worker.py','agent_stability_proxy.py')
    return {name:hashlib.sha256((ROOT/'tests/manual'/name).read_bytes()).hexdigest() for name in names}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--prior-result',type=Path,default=ROOT/'artifacts/named-probe-review-quality-12b-20261011-02/result.json')
    parser.add_argument('--upstream',default='http://127.0.0.1:5001')
    parser.add_argument('--seeds',nargs='+',type=int,default=[91,92])
    parser.add_argument('--max-turns',type=int,default=24)
    parser.add_argument('--max-tokens',type=int,default=1536)
    parser.add_argument('--request-limit',type=int,default=48)
    parser.add_argument('--timeout',type=int,default=360)
    parser.add_argument('--temperature',type=float,default=.2)
    options=parser.parse_args()
    options.upstream=local_url(options.upstream)
    prior=json.loads(options.prior_result.read_text(encoding='utf-8'))
    replay=eligible_replays(prior)
    selected=cases()
    from codey.task.entry_auth import derive_entry_auth
    for case in selected:
        if 'project.write' in derive_entry_auth({'task':case.task,'intent':'project'},project='fixture').denied_capabilities:
            raise ValueError('fixture does not authorize writing')
    with urlopen(options.upstream+'/v1/models',timeout=5) as response:
        model=json.load(response)['data'][0]['id']
    with urlopen(options.upstream+'/api/extra/perf',timeout=5) as response:
        perf=json.load(response)
    if model!=prior['plan']['model'] or perf.get('idle')!=1 or perf.get('queue')!=0:
        raise ValueError('same model must be loaded and idle before dispatch')
    production=production_hashes()
    if production!=prior['plan']['production_hashes']:
        raise ValueError('production baseline identity changed')
    scripts=frozen_hashes()
    options.run_dir=options.run_dir.resolve()
    options.run_dir.mkdir(parents=True,exist_ok=False)
    plan={'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'model':model,'controls':vars(options)|{'run_dir':str(options.run_dir),'prior_result':str(options.prior_result)},
        'production_hashes':production,'script_hashes':scripts,'before_perf':perf,
        'prior_result_sha256':hashlib.sha256(options.prior_result.read_bytes()).hexdigest(),
        'replay_pairs':[(c.name,s) for c,s,_ in replay],
        'replay_plan_hashes':{f'{c.name}/{s}':digest(json.dumps(p,sort_keys=True)) for c,s,p in replay},
        'holdouts':[{'name':c.name,'task':c.task,'candidate_sha256':digest(c.candidate),'oracle_sha256':digest(c.oracle)} for c in selected],
        'stages':{'replay':len(replay)*3,'proposal':len(selected)*len(options.seeds),
                  'semantic':len(selected)*len(options.seeds)*3,'repair':len(selected)*len(options.seeds)*3},
        'rules':['Only known usable prior plans enter targeted replay; all seven included regardless of verdict.',
            'Targeted replay is development evidence, not unseen-task generalization.',
            'New model proposals frozen once before all new-task final judgments.',
            'Exposed and fact-only final groups see identical inputs and actual execution method.',
            'Fact-only allowlists inputs/actual values; no expected values, narratives, errors or raw plans.',
            'Frozen author candidates; actual Local reviewer/repair; no oracle online.',
            'Current baseline rerun as a predeclared replication, not failure hunting.',
            'Repair budgets raised uniformly in advance; historical 12-turn rates not directly comparable.',
            'Unknown generation halts; no resend, no invalid outcome exclusions or prompt retuning.']}
    (options.run_dir/'preregistered.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    proxy=Proxy(('127.0.0.1',0),options.upstream,options.timeout,journal=options.run_dir/'http.jsonl',request_limit=options.request_limit)
    thread=threading.Thread(target=proxy.serve_forever,daemon=True)
    thread.start()
    rows=[]
    prepared={}
    def save():
        (options.run_dir/'result.json').write_text(json.dumps({'plan':plan,'runs':rows,'summary':summary(rows)},ensure_ascii=False,indent=2),encoding='utf-8')
    def execute(case,seed,arm,stage,shared=None):
        directory=options.run_dir/f'{stage}-{case.name}-{seed}-{arm}'
        try:
            row=run_arm(case,seed,arm,'repair' if stage=='repair' else 'discovery',directory,proxy,options,model,
                worker_module=WORKER,worker_phase='plan' if stage=='proposal' else None,
                extra_manifest={'prepared_plan':shared} if shared else None,
                evidence_scorer=replay_score if stage=='replay' else score_probe)
        except Exception as exc:
            with proxy.record_lock:
                records=json.loads(json.dumps([r for r in proxy.records if r.get('arm')==f"{'repair' if stage=='repair' else 'discovery'}/{case.name}/{seed}/{arm}"]))
            row={'stage':stage,'case':case.name,'cohort':case.cohort,'buggy':case.buggy,'seed':seed,'arm':arm,
                 'seconds':None,'process_status':'parent_record_incomplete','success':None,
                 'usage':usage_totals(records),'error':type(exc).__name__+': '+str(exc),
                 'evidence':json_lines(directory/'state/counterexamples.jsonl')}
            rows.append(row)
            save()
            (directory/'incomplete-record.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        row['stage']=stage
        rows.append(row)
        save()
        print(json.dumps({'stage':stage,'case':case.name,'seed':seed,'arm':arm,'verdict':row['first_review']['verdict'],
                          'success':row['success'],'reason':row['terminal']['reason'],'seconds':round(row['seconds'],2)}),flush=True)
        if row['harness_error']:
            raise RuntimeError('missing child observation; halted')
        return row
    save()
    try:
        for index,(case,seed,shared) in enumerate(replay):
            for arm in ordered_arms(index):
                execute(case,seed,arm,'replay',shared if arm!='codey' else None)
        for case in selected:
            for seed in options.seeds:
                row=execute(case,seed,'plan-native','proposal')
                prepared[(case.name,seed)]=row['reviews'][0]['review']['recorded_plan']
        for stage in ('semantic','repair'):
            for index,case in enumerate(selected):
                for seed in options.seeds:
                    for arm in ordered_arms(index+options.seeds.index(seed)):
                        execute(case,seed,arm,stage,prepared[(case.name,seed)] if arm!='codey' else None)
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)
        save()
        if production_hashes()!=production or frozen_hashes()!=scripts:
            raise RuntimeError('production or frozen experimental scripts changed')


if __name__=='__main__':
    main()
