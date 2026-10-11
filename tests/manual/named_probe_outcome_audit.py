"""Offline lifecycle and measured incremental-cost audit of the fixed experiment."""
import argparse
import json
from pathlib import Path


def analyze(data):
    rows=data['runs']
    finals=[r for r in rows if r['stage'] in ('semantic','repair')]
    proposal={(r['case'],r['seed']):r for r in rows if r['stage']=='proposal'}
    result={'repair_details':[], 'incremental_costs':{}}
    for row in finals:
        if row['stage']=='repair':
            result['repair_details'].append({'case':row['case'],'seed':row['seed'],'arm':row['arm'],
                'success':row['success'],'qualified_patch':row['qualified_patch'],
                'reason':row['terminal']['reason'],'false_completion':row['false_completion'],
                'visible_passed':row['verification']['visible_passed'],
                'hidden_passed':row['verification']['hidden_passed'],
                'scope_ok':row['verification']['scope_ok'],
                'fresh':row['verification']['agent_verification_fresh'],
                'reviews':[{'verdict':x.get('review',{}).get('verdict'),
                            'status':x.get('review',{}).get('status')} for x in row['reviews']]})
    for stage in ('semantic','repair'):
        result['incremental_costs'][stage]={}
        for arm in sorted({r['arm'] for r in finals if r['stage']==stage}):
            selected=[r for r in finals if r['stage']==stage and r['arm']==arm]
            native=arm.startswith('native-')
            # Include real selection cost for independent deployment of either method.
            # The shared proposal is NOT counted as free just because the experiment cached it.
            plans=[proposal[(r['case'],r['seed'])] for r in selected] if native else []
            known=sum(r['usage']['known_token_usage'] or 0 for r in selected+plans)
            complete=all(r['seconds'] is not None and (r['usage']['usage_complete'] or
                         r['usage']['physical_generation_exchanges']==0) for r in selected+plans)
            result['incremental_costs'][stage][arm]={
                'final_rows':len(selected),'known_tokens_including_initial_selection':known,
                'total_tokens':known if complete else None,
                'cost_complete':complete,
                'known_seconds_including_initial_selection':sum(r['seconds'] or 0 for r in selected+plans),
                'limitation':'Frozen candidate writing cost excluded; restart/cold-cache timing not normalized.'}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    data=json.loads((args.directory/'result.json').read_text(encoding='utf-8'))
    result=analyze(data)
    (args.directory/'outcome-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
