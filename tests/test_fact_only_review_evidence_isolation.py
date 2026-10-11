"""Final reviewer must receive observations, never the proposer's interpretations."""
import json

from tests.manual.fact_only_review_adapter import FactOnlyReviewer, observation_packet
from tests.manual.named_probe_review_adapter import validate_plan

SOURCE='def select(text,needle): return 3\n'
TASK='Count all occurrences, including overlaps.'
SENTINEL='UNTRUSTED_PROPOSER_INTERPRETATION_999'


def prepared():
    row=validate_plan({'probes':[{'requirement_quote':TASK,'preconditions':SENTINEL,
        'alternative':SENTINEL,'arguments':{'text':'abababa','needle':'aba'},'expected':999}]},SOURCE,TASK)
    row.update({'raw_plan':SENTINEL,'errors':[{'probe':SENTINEL}],'protocol_usable':False,
        'actual':{'observations':[{'status':'returned','value':3}],
                  'source_sha256':'hash','final_source_sha256':'hash'}})
    return row


def test_allowlist_removes_all_proposer_interpretations_and_keeps_actual_input():
    packet=observation_packet(prepared())
    assert SENTINEL not in json.dumps(packet) and '999' not in json.dumps(packet)
    assert packet['samples']==[{'input':{'args':[],'kwargs':{'text':'abababa','needle':'aba'}},
                               'actual':{'status':'returned','value':3}}]


def test_actual_json_data_is_not_filtered_by_its_legitimate_key_names():
    row=prepared()
    row['actual']['observations'][0]['value']={'expected':False,'alternative':[0,None]}
    assert observation_packet(row)['samples'][0]['actual']['value']=={'expected':False,'alternative':[0,None]}


def test_no_observation_does_not_derive_pass_fail_from_expectation():
    row=prepared()
    row['actual']=None
    packet=observation_packet(row)
    assert packet['samples']==[] and packet['availability']=='not_observed'
    assert 'expected' not in json.dumps(packet) and 'passed' not in json.dumps(packet)


def test_fixed_exception_observation_survives_without_proposed_diagnosis():
    row=prepared()
    row['actual']['observations']=[{'status':'raised','exception_type':'ValueError','message':'bad input'}]
    packet=observation_packet(row)
    assert packet['samples'][0]['actual']==row['actual']['observations'][0]
    assert SENTINEL not in json.dumps(packet)


def test_fact_only_reviewer_starts_fresh_and_never_sends_plan_narrative(tmp_path):
    (tmp_path/'app.py').write_text(SOURCE)
    class Delegate:
        def __init__(self):
            self.prompts=[]
            self.resets=0
        def new_chat(self,*a,**kw):
            self.resets+=1
        def send(self,prompt,**kw):
            self.prompts.append(prompt)
            return '{"verdict":"approved","summary":"independent judgment","findings":[]}'
    delegate=Delegate()
    reviewer=FactOnlyReviewer(delegate,tmp_path,TASK,tmp_path/'log.jsonl',execute=True,prepared_plan=prepared())
    original='Original user task:\n'+TASK+'\nWriter summary:\nWriter claim'
    reviewer.send(original)
    assert len(delegate.prompts)==1 and delegate.resets==1
    assert SENTINEL not in delegate.prompts[0] and '999' not in delegate.prompts[0]
    assert original in delegate.prompts[0]
    row=json.loads((tmp_path/'log.jsonl').read_text())
    assert row['hypotheses'][0]['probe']['expected']==999  # Retained offline, never erased or treated as oracle.
    assert row['sent_evidence']['samples'][0]['actual']['value']==3
    reviewer.send('Format repair only')
    assert delegate.prompts[-1]=='Format repair only' and delegate.resets==1


def test_pairing_or_source_changes_reject_packet_instead_of_guessing():
    import pytest
    row=prepared()
    row['actual']['observations']*=2
    with pytest.raises(ValueError,match='pairing'):
        observation_packet(row)
    row=prepared()
    row['actual']['final_source_sha256']='different'
    with pytest.raises(ValueError,match='source changed'):
        observation_packet(row)


def test_holdout_controls_and_bugs_both_pass_visible_tests(monkeypatch):
    import sys
    import types
    import unittest

    import pytest

    from tests.manual.fact_only_review_holdout_cases import cases
    for case in cases():
        module=types.ModuleType('app')
        exec(case.candidate,module.__dict__)
        monkeypatch.setitem(sys.modules,'app',module)
        visible=types.ModuleType('visible')
        exec(case.files['test_app.py'],visible.__dict__)
        result=unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromModule(visible).run(result)
        assert result.wasSuccessful(),case.name
        if case.buggy:
            with pytest.raises(AssertionError):
                exec(case.oracle,{'m':module})
        else:
            exec(case.oracle,{'m':module})


def test_replay_selection_uses_proposal_availability_not_verdict_or_success():
    from tests.manual.fact_only_review_quality_ab import eligible_replays
    prior={'plan':{'seeds':[81]},'runs':[{'stage':'proposal','case':'rightmost-match-bug','seed':81,
        'seconds':1,'success':False,'first_review':{'verdict':'approved'},
        'reviews':[{'review':{'recorded_plan':{'protocol_usable':True}}}]}]}
    assert len(eligible_replays(prior))==1
    prior['runs'][0]['seconds']=None
    assert eligible_replays(prior)==[]


def test_continuation_skips_unknown_attempt_and_keeps_frozen_remaining_order():
    from tests.manual.fact_only_review_holdout_cases import cases
    from tests.manual.fact_only_review_resume_ab import pending_jobs
    selected=cases()
    done=[{'stage':'proposal','case':selected[1].name,'seed':92,'arm':'plan-native','seconds':None}]
    jobs=pending_jobs([],selected,[91,92],done)
    assert len(jobs)==55
    assert (selected[1],92,'plan-native','proposal') not in jobs
    assert sum(stage=='proposal' for _,_,_,stage in jobs)==7
    assert sum(stage=='semantic' for _,_,_,stage in jobs)==24
    assert sum(stage=='repair' for _,_,_,stage in jobs)==24


def test_archived_unknown_plan_preserves_unavailability_without_model_retry(tmp_path):
    from tests.manual.fact_only_review_holdout_cases import cases
    from tests.manual.fact_only_review_resume_ab import archived_plan
    from tests.manual.open_code_review_self_review_ab import digest
    case=cases()[1]
    path=tmp_path/f'proposal-{case.name}-92-plan-native/state'
    path.mkdir(parents=True)
    unknown={'source_text_sha256':digest(case.candidate),'protocol_usable':False,'hypotheses':[],
             'errors':['GenerationUnknownError: generation outcome unknown; request not resent']}
    (path/'discovery.json').write_text(json.dumps({'recorded_plan':unknown}))
    assert archived_plan({'seconds':None,'seed':92},tmp_path,case)==unknown


def test_repaired_source_regenerates_plan_instead_of_reusing_old_hypothesis(tmp_path,monkeypatch):
    from tests.manual.counterexample_review_adapter import digest
    changed='def select(text,needle): return 4\n'
    (tmp_path/'app.py').write_text(changed)
    seen=[]
    def new_plan(delegate,project,task,prompt,**kw):
        seen.append((project/'app.py').read_text())
        return {'hypotheses':[],'errors':[],'source_text_sha256':digest(changed.encode())}
    monkeypatch.setattr('tests.manual.fact_only_review_adapter.generate_plan',new_plan)
    class Delegate:
        def new_chat(self,*a,**kw):
            pass
        def send(self,prompt,**kw):
            assert SENTINEL not in prompt
            return '{"verdict":"approved","summary":"fixture","findings":[]}'
    reviewer=FactOnlyReviewer(Delegate(),tmp_path,TASK,tmp_path/'log.jsonl',execute=True,prepared_plan=prepared())
    reviewer.send('Current task and source')
    row=json.loads((tmp_path/'log.jsonl').read_text())
    assert seen==[changed] and row['plan_origin']=='new_current_source_proposal'
    assert row['sent_evidence']['availability']=='not_observed'
