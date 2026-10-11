"""Resume the frozen evidence-isolation study without resending unknown generations."""
import argparse
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

from tests.manual.agent_stability_measurements import usage_totals
from tests.manual.agent_stability_proxy import Proxy, local_url
from tests.manual.counterexample_review_quality_ab import run_arm
from tests.manual.fact_only_review_holdout_cases import cases, score_probe
from tests.manual.fact_only_review_quality_ab import WORKER, eligible_replays, frozen_hashes, ordered_arms, summary
from tests.manual.named_probe_experiment_recovery import completed_keys
from tests.manual.named_probe_resume_quality_ab import production_hashes
from tests.manual.named_probe_review_cases import score_probe as replay_score
from tests.manual.open_code_review_self_review_ab import digest


def pending_jobs(replay,selected,seeds,rows):
    done=completed_keys(rows)
    jobs=[]
    for index,(case,seed,_) in enumerate(replay):
        jobs.extend((case,seed,arm,'replay') for arm in ordered_arms(index)
                    if ('replay',case.name,seed,arm) not in done)
    for case in selected:
        jobs.extend((case,seed,'plan-native','proposal') for seed in seeds
                    if ('proposal',case.name,seed,'plan-native') not in done)
    for stage in ('semantic','repair'):
        for index,case in enumerate(selected):
            for seed in seeds:
                jobs.extend((case,seed,arm,stage) for arm in ordered_arms(index+seeds.index(seed))
                            if (stage,case.name,seed,arm) not in done)
    return jobs


def archived_plan(row,source,case):
    if row.get('seconds') is not None:
        return row['reviews'][0]['review']['recorded_plan']
    path=source/f"proposal-{case.name}-{row['seed']}-plan-native"/'state/discovery.json'
    plan=json.loads(path.read_text(encoding='utf-8'))['recorded_plan']
    if plan['source_text_sha256']!=digest(case.candidate) or plan.get('protocol_usable') or not any(
            'outcome unknown' in str(e) for e in plan.get('errors',[])):
        raise ValueError('incomplete proposal has no matching archived unknown generation')
    return plan


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir',type=Path,required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args()
    source=args.source_dir.resolve()
    data=json.loads((source/'result.json').read_text(encoding='utf-8'))
    plan=data['plan']
    prior=json.loads(Path(plan['controls']['prior_result']).read_text(encoding='utf-8'))
    replay=eligible_replays(prior)
    selected=cases()
    byname={c.name:c for c in selected}
    rows=data['runs']
    prepared={(r['case'],r['seed']):archived_plan(r,source,byname[r['case']]) for r in rows if r['stage']=='proposal'}
    if production_hashes()!=plan['production_hashes'] or frozen_hashes()!=plan['script_hashes']:
        raise ValueError('production or frozen experiment identity changed')
    options=SimpleNamespace(**plan['controls'])
    options.upstream=local_url(options.upstream)
    options.run_dir=args.run_dir.resolve()
    with urlopen(options.upstream+'/v1/models',timeout=5) as response:
        model=json.load(response)['data'][0]['id']
    with urlopen(options.upstream+'/api/extra/perf',timeout=5) as response:
        perf=json.load(response)
    if model!=plan['model'] or perf.get('idle')!=1 or perf.get('queue')!=0:
        raise ValueError('same model must be loaded and idle')
    options.run_dir.mkdir(parents=True,exist_ok=False)
    audit={'source_result':str(source/'result.json'),'source_sha256':hashlib.sha256((source/'result.json').read_bytes()).hexdigest(),
           'prior_rows_preserved':len(rows),'before_perf':perf,
           'pending':[(c.name,s,a,t) for c,s,a,t in pending_jobs(replay,selected,options.seeds,rows)],
           'resume_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           'rule':'Preserve all prior rows, including incomplete record. Unknown proposal is unavailable, never regenerated.'}
    (options.run_dir/'continuation.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    proxy=Proxy(('127.0.0.1',0),options.upstream,options.timeout,journal=options.run_dir/'http.jsonl',request_limit=options.request_limit)
    thread=threading.Thread(target=proxy.serve_forever,daemon=True)
    thread.start()
    def save():
        (options.run_dir/'result.json').write_text(json.dumps({'plan':plan,'continuation':audit,'runs':rows,
            'summary':summary(rows)},ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        replay_plans={(c.name,s):p for c,s,p in replay}
        for case,seed,arm,stage in pending_jobs(replay,selected,options.seeds,rows):
            shared=(replay_plans if stage=='replay' else prepared).get((case.name,seed)) if arm!='codey' else None
            directory=options.run_dir/f'{stage}-{case.name}-{seed}-{arm}'
            try:
                row=run_arm(case,seed,arm,'repair' if stage=='repair' else 'discovery',directory,proxy,options,model,
                    worker_module=WORKER,worker_phase='plan' if stage=='proposal' else None,
                    extra_manifest={'prepared_plan':shared} if shared else None,
                    evidence_scorer=replay_score if stage=='replay' else score_probe)
            except Exception as exc:
                with proxy.record_lock:
                    records=json.loads(json.dumps([r for r in proxy.records if r.get('arm')==f"{'repair' if stage=='repair' else 'discovery'}/{case.name}/{seed}/{arm}"]))
                rows.append({'stage':stage,'case':case.name,'cohort':case.cohort,'buggy':case.buggy,'seed':seed,
                             'arm':arm,'seconds':None,'success':None,'process_status':'parent_record_incomplete',
                             'usage':usage_totals(records),'error':str(exc),'evidence':[]})
                save()
                (options.run_dir/'continuation-error.json').write_text(json.dumps({'stage':stage,'case':case.name,
                    'seed':seed,'arm':arm,'error':str(exc),'rule':'Do not retry this directory or unknown generation.'}),encoding='utf-8')
                raise
            row['stage']=stage
            rows.append(row)
            if stage=='proposal':
                prepared[(case.name,seed)]=row['reviews'][0]['review']['recorded_plan']
            save()
            print(json.dumps({'stage':stage,'case':case.name,'seed':seed,'arm':arm,
                              'verdict':row['first_review']['verdict'],'success':row['success'],
                              'reason':row['terminal']['reason'],'seconds':round(row['seconds'],2)}),flush=True)
            if row['harness_error']:
                raise RuntimeError('missing observation; continuation halted')
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)
        save()
        if production_hashes()!=plan['production_hashes'] or frozen_hashes()!=plan['script_hashes']:
            raise RuntimeError('production or frozen experiment changed')


if __name__=='__main__':
    main()
