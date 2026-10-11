"""Continue fixed semantic/repair stages without retrying completed or unknown requests."""
import argparse
import hashlib
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

from tests.manual.agent_stability_proxy import Proxy, local_url
from tests.manual.counterexample_review_quality_ab import ROOT, run_arm
from tests.manual.named_probe_experiment_recovery import completed_keys, recover_unknown_proposal
from tests.manual.named_probe_protocol_and_quality_ab import WORKER, script_hashes, summary
from tests.manual.named_probe_review_cases import cases, score_probe


def pending_stages(selected, seeds, rows):
    done=completed_keys(rows)
    jobs=[]
    for stage in ('semantic','repair'):
        for index,case in enumerate(selected):
            for seed in seeds:
                arms=['codey','native-data','native-observed'] if stage=='semantic' else ['no-review','codey','native-observed']
                shift=(index+seeds.index(seed))%len(arms)
                for arm in arms[shift:]+arms[:shift]:
                    if (stage,case.name,seed,arm) not in done:
                        jobs.append((case,seed,arm,stage))
    return jobs


def honest_summary(rows):
    # Unknown infrastructure rows have no semantic verdict or measurable total cost.
    measured=[r for r in rows if r.get('seconds') is not None]
    return {'measured':summary(measured),
            'unknown_attempts':[{'stage':r['stage'],'case':r['case'],'seed':r['seed'],'arm':r['arm'],
                                 'reason':r.get('infrastructure_error')} for r in rows if r.get('seconds') is None],
            'total_rows':len(rows),'all_costs_known':len(measured)==len(rows)}


def production_hashes():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT/'codey').rglob('*') if p.is_file() and '__pycache__' not in p.parts}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir',type=Path,required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args()
    source=args.source_dir.resolve()
    prior=json.loads((source/'result.json').read_text(encoding='utf-8'))
    plan=prior['plan']
    selected=cases()
    rows=prior['runs']
    for case in selected:
        for seed in plan['seeds']:
            key=('proposal',case.name,seed,'plan-native')
            if key not in completed_keys(rows):
                rows.append(recover_unknown_proposal(case,seed,source/f'proposal-{case.name}-{seed}-plan-native',source/'http.jsonl'))
    prepared={(r['case'],r['seed']):r['reviews'][0]['review']['recorded_plan'] for r in rows if r['stage']=='proposal'}
    current_scripts=script_hashes()
    changes={name:{'before':old,'after':current_scripts[name]} for name,old in plan['script_hashes'].items()
             if current_scripts[name]!=old}
    if set(changes)!={'counterexample_review_quality_ab.py'}:
        raise ValueError('unexpected frozen script change')
    if production_hashes()!=plan['production_hashes']:
        raise ValueError('production identity changed')
    options=SimpleNamespace(**plan['controls'])
    options.upstream=local_url(options.upstream)
    options.run_dir=args.run_dir.resolve()
    with urlopen(options.upstream+'/v1/models',timeout=5) as response:
        model=json.load(response)['data'][0]['id']
    if model!=plan['model']:
        raise ValueError('loaded model does not match original experiment')
    with urlopen(options.upstream+'/api/extra/perf',timeout=5) as response:
        perf=json.load(response)
    if perf.get('idle')!=1 or perf.get('queue')!=0:
        raise ValueError('backend is not idle; no inference dispatched')
    options.run_dir.mkdir(parents=True,exist_ok=False)
    audit={'original_plan':str(source/'preregistered.json'),'original_result':str(source/'result.json'),
           'production_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
           'model':model,'before_resume_perf':perf,'script_changes':changes,
           'correction':'Offline scorer previously indexed a missing observation in data-only arm. It now records null; no model behavior changes.',
           'new_script_hashes':{name:hashlib.sha256((ROOT/'tests/manual'/name).read_bytes()).hexdigest()
               for name in ('named_probe_experiment_recovery.py','named_probe_resume_quality_ab.py','named_probe_text_envelope_audit.py')},
           'rules':['Keep prior 23 rows unchanged; archive 24th as unknown without retry.',
                    'Do not regenerate the unknown shared proposal; associated finals see unavailable proposal.',
                    'Same preregistered remaining order, tasks, seeds, sampling and limits.',
                    'Restarts and unknown costs preclude complete aggregate time/token comparison.'],
           'pending':[(c.name,s,a,t) for c,s,a,t in pending_stages(selected,plan['seeds'],rows)]}
    (options.run_dir/'continuation.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    proxy=Proxy(('127.0.0.1',0),options.upstream,options.timeout,journal=options.run_dir/'http.jsonl',request_limit=options.request_limit)
    thread=threading.Thread(target=proxy.serve_forever,daemon=True)
    thread.start()
    def save():
        (options.run_dir/'result.json').write_text(json.dumps({'plan':plan,'continuation':audit,'runs':rows,
            'summary':honest_summary(rows)},ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        for case,seed,arm,stage in pending_stages(selected,plan['seeds'],rows):
            shared=prepared[(case.name,seed)] if arm.startswith('native-') else None
            row=run_arm(case,seed,arm,'repair' if stage=='repair' else 'discovery',
                options.run_dir/f'{stage}-{case.name}-{seed}-{arm}',proxy,options,model,
                worker_module=WORKER,extra_manifest={'prepared_plan':shared} if shared else None,evidence_scorer=score_probe)
            row['stage']=stage
            rows.append(row)
            save()
            print(json.dumps({'stage':stage,'case':case.name,'seed':seed,'arm':arm,
                              'verdict':row['first_review']['verdict'],'success':row['success'],
                              'seconds':round(row['seconds'],2)}),flush=True)
            if row['harness_error']:
                raise RuntimeError('missing observation; continuation halted')
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)
        save()
        if production_hashes()!=plan['production_hashes'] or script_hashes()!=current_scripts:
            raise RuntimeError('production or continuation scripts changed during experiment')


if __name__=='__main__':
    main()
