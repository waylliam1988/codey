"""Offline evidence isolation, incremental cost and lifecycle audit."""
import argparse
import json
from pathlib import Path

from tests.manual.fact_only_review_adapter import observation_packet


def analyze(data,prior):
    rows=data['runs']
    old_plans={(r['case'],r['seed']):r for r in prior['runs'] if r['stage']=='proposal' and r['seconds'] is not None}
    plans={(r['case'],r['seed']):r for r in rows if r['stage']=='proposal'}
    result={'incremental_costs':{},'repair_details':[],'fact_packets_checked':0,'isolation_mismatches':[]}
    for row in rows:
        if row['arm']=='fact-only':
            for index,evidence in enumerate(row.get('evidence',[])):
                result['fact_packets_checked']+=1
                if evidence.get('sent_evidence')!=observation_packet(evidence):
                    result['isolation_mismatches'].append((row['stage'],row['case'],row['seed'],index))
        if row['stage']=='repair':
            if row['seconds'] is None:
                result['repair_details'].append({'case':row['case'],'seed':row['seed'],'arm':row['arm'],
                    'available':False,'success':None,'false_completion':None,'error':row['error']})
                continue
            result['repair_details'].append({'case':row['case'],'seed':row['seed'],'arm':row['arm'],'available':True,
                'success':row['success'],'qualified_patch':row['qualified_patch'],'reason':row['terminal']['reason'],
                'hidden_passed':row['verification']['hidden_passed'],'scope_ok':row['verification']['scope_ok'],
                'fresh':row['verification']['agent_verification_fresh'],'changed_paths':row['verification']['changed_paths'],
                'first_review':row['first_review'],'review_count':len(row['reviews']),
                'false_completion':row['false_completion']})
    for stage in ('replay','semantic','repair'):
        result['incremental_costs'][stage]={}
        for arm in ('codey','exposed','fact-only'):
            selected=[r for r in rows if r['stage']==stage and r['arm']==arm]
            table=old_plans if stage=='replay' else plans
            prefixes=[table[(r['case'],r['seed'])] for r in selected] if arm!='codey' else []
            charged=selected+prefixes
            known=sum(r['usage']['known_token_usage'] or 0 for r in charged)
            complete=all(r['usage']['usage_complete'] and r['seconds'] is not None for r in charged)
            result['incremental_costs'][stage][arm]={'rows':len(selected),'known_tokens':known,
                'total_tokens':known if complete else None,'usage_complete':complete,
                'seconds_including_selection':sum(r['seconds'] or 0 for r in charged),
                'limitations':'Fixed author cost omitted; initial shared plan charged separately to each deployed method; cold cache timings not normalized.'}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    data=json.loads((args.directory/'result.json').read_text(encoding='utf-8'))
    prior=json.loads(Path(data['plan']['controls']['prior_result']).read_text(encoding='utf-8'))
    result=analyze(data,prior)
    (args.directory/'outcome-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='repair_details'},ensure_ascii=False))


if __name__=='__main__':
    main()
