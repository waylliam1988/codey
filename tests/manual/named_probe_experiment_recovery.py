"""Archive a lost parent result without retrying an unknown generation."""
import json

from tests.manual.agent_stability_measurements import usage_totals
from tests.manual.open_code_review_self_review_ab import digest, first_review


def recover_unknown_proposal(case, seed, directory, journal):
    review = json.loads((directory/'state/discovery.json').read_text(encoding='utf-8'))
    plan = review['recorded_plan']
    if plan['source_text_sha256'] != digest(case.candidate):
        raise ValueError('unknown proposal source identity does not match the frozen candidate')
    if plan.get('protocol_usable') or not any('outcome unknown' in str(e) for e in plan.get('errors',[])):
        raise ValueError('this recovery accepts only an archived unknown generation')
    arm_label=f'discovery/{case.name}/{seed}/plan-native'
    records=[json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
    records=[r for r in records if r.get('arm')==arm_label]
    return {'case':case.name,'cohort':case.cohort,'buggy':case.buggy,'seed':seed,'arm':'plan-native',
        'phase':'discovery','stage':'proposal','candidate_sha256':digest(case.candidate),
        'final_sha256':None,'process_status':'parent_result_unavailable','returncode':None,'seconds':None,
        'terminal':{'completed':False,'reason':'infrastructure_unknown'},
        'verification':{'visible_passed':None,'hidden_passed':None,'scope_ok':None,'agent_verification_fresh':None},
        'first_review':first_review([{'review':review}]),'reviews':[{'review':review}],
        'evidence':[plan],'offline_probe_scores':[],'usage':usage_totals(records),
        'completion_proofs':[],'qualified_patch':None,'completion_proof_present':False,'success':False,
        'false_completion':False,'harness_error':False,
        'infrastructure_error':'Backend stayed busy after an unknown generation; no retry.',
        'recovery_note':'Parent lost exact timing/exit result before writing its row. Missing facts remain unknown.'}


def completed_keys(rows):
    keys=[(r['stage'],r['case'],r['seed'],r['arm']) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate experiment identities')
    return set(keys)
