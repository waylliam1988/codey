"""Post-hoc offline audit of JSON envelopes; never changes live experiment rows."""
import argparse
import json
from pathlib import Path

from tests.manual.counterexample_review_cases import cases, domain, score_probe
from tests.manual.named_probe_review_adapter import validate_plan


def remove_json_envelope(text):
    """Remove only a complete outer Markdown fence, without repairing model JSON."""
    lines=text.strip().splitlines()
    if len(lines)>=3 and lines[0].strip() in ('```json','```') and lines[-1].strip()=='```':
        return '\n'.join(lines[1:-1]), True
    return text.strip(), False


def audit(directory):
    data=json.loads((directory/'result.json').read_text(encoding='utf-8'))
    fixtures={c.name:c for c in cases()}
    rows=[]
    for row in data['runs']:
        if row['stage']!='protocol' or row['arm']!='plan-text':
            continue
        records=json.loads((directory/f"protocol-{row['case']}-{row['seed']}-plan-text"/'requests.json').read_text(encoding='utf-8'))
        content=next((r['response']['choices'][0]['message'].get('content') or '' for r in records
                      if r.get('path')=='/v1/chat/completions' and r.get('response_complete')), '')
        body,fenced=remove_json_envelope(content)
        case=fixtures[row['case']]
        try:
            plan=validate_plan(json.loads(body),case.candidate,case.task)
        except (ValueError,TypeError) as exc:
            plan={'errors':[str(exc)],'hypotheses':[]}
        namespace={}
        exec(case.candidate,namespace)  # Fixed authored fixture, no model-generated code.
        scores=[]
        for hypothesis in plan['hypotheses']:
            probe=hypothesis['probe']
            if domain(case,probe):
                try:
                    actual={'status':'returned','value':namespace['select'](*probe['args'],**probe['kwargs'])}
                except Exception as exc:
                    actual={'status':'raised','type':type(exc).__name__}
            else:
                actual={'status':'not_executed_invalid_domain'}
            scores.append({'probe':probe,'actual':actual,**score_probe(case,probe,actual)})
        rows.append({'case':case.name,'seed':row['seed'],'outer_json_fence':fenced,
                     'usable_after_envelope_removal':not plan.get('errors'),
                     'errors':plan.get('errors',[]),'scores':scores})
    return {'scope':'Post-hoc offline envelope audit; no model retry or live result replacement.',
            'runs':rows,'usable':sum(r['usable_after_envelope_removal'] for r in rows),
            'valid_counterexamples':sum(p['expected_correct'] is True and p['distinguishes']
                                       for r in rows for p in r['scores'])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    result=audit(args.directory)
    (args.directory/'text-envelope-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='runs'},ensure_ascii=False))


if __name__=='__main__':
    main()
