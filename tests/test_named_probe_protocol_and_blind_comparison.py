"""Named probe schema and observation ablations must not manufacture truth."""
import pytest

from tests.manual.named_probe_review_adapter import probe_schema, proposal_prompt, validate_plan

SOURCE = 'def select(value, low=None, high=None): return value\n'
TASK = 'Zero is a valid bound.'


def plan(**kw):
    return {'probes': [{'requirement_quote': TASK, 'preconditions': 'integer value',
                        'arguments': {'value': -2, 'low': 0}, 'expected': 0,
                        'alternative': 'bound treated as absent', **kw}]}


def test_schema_derives_parameter_names_and_optional_arguments_without_import():
    schema = probe_schema('raise AssertionError("no import")\n' + SOURCE)
    arguments = schema['properties']['probes']['items']['properties']['arguments']
    assert set(arguments['properties']) == {'value', 'low', 'high'}
    assert arguments['required'] == ['value']
    assert arguments['additionalProperties'] is False


def test_named_arguments_bind_but_do_not_admit_model_expectations():
    row = validate_plan(plan(expected=123), SOURCE, TASK)
    assert row['hypotheses'][0]['probe']['kwargs'] == {'value': -2, 'low': 0}
    assert row['hypotheses'][0]['semantic_admission'] == 'unverified'
    assert 'passed' not in str(row)


@pytest.mark.parametrize('data', [plan(arguments={}), plan(arguments={'value': 1, 'unknown': 3}),
    plan(arguments='select(1)'), plan(requirement_quote='invented'), {'probes': [*plan()['probes']]*3}])
def test_invalid_binding_or_quote_remains_unusable(data):
    row = validate_plan(data, SOURCE, TASK)
    assert not row['hypotheses']
    assert row['errors']


def test_output_prompt_has_only_one_format_and_preserves_original_task_and_summary():
    original = ('You are reviewer. Return only JSON.\n{"verdict":"approved"}\n'
                'Project: fixture\nOriginal user task:\nRequirement\nWriter summary:\nWriter claim\nDiff:\n+code')
    prompt = proposal_prompt(original, probe_schema(SOURCE), native=True)
    assert '"verdict"' not in prompt
    assert 'Original user task:\nRequirement' in prompt and 'Writer summary:\nWriter claim' in prompt
    assert 'submit_probe_plan' in prompt


def test_same_real_proposal_is_shared_but_only_observed_arm_executes(tmp_path):
    import json

    from tests.manual.named_probe_review_adapter import NamedProbeReviewer
    (tmp_path/'app.py').write_text(SOURCE)
    prepared=validate_plan(plan(),SOURCE,TASK)
    class Delegate:
        def __init__(self):
            self.prompts=[]
        def new_chat(self,*a,**kw):
            pass
        def send(self,prompt,**kw):
            self.prompts.append(prompt)
            return '{"verdict":"approved","summary":"delegate judgment","findings":[]}'
    rows=[]
    for mode in (False,True):
        delegate=Delegate()
        log=tmp_path/f'{mode}.jsonl'
        reviewer=NamedProbeReviewer(delegate,tmp_path,TASK,log,execute=mode,prepared_plan=prepared)
        assert json.loads(reviewer.send('Original requirements and diff'))['verdict']=='approved'
        row=json.loads(log.read_text())
        assert row['plan_origin']=='shared_real_model_proposal'
        assert len(delegate.prompts)==1  # No regeneration of the shared plan.
        rows.append(row)
    assert rows[0]['hypotheses']==rows[1]['hypotheses']
    assert rows[0]['actual'] is None
    assert rows[1]['actual']['observations'][0]['value']==-2
    assert all(h['semantic_admission']=='unverified' for h in rows[1]['hypotheses'])


def test_offline_json_scoring_preserves_boolean_and_integer_distinction():
    from tests.manual.named_probe_review_cases import same_json
    assert not same_json({'x':False},{'x':0})
    assert same_json({'x':1,'y':2},{'y':2,'x':1})


def test_new_fixture_controls_and_faults_share_task_and_visible_validation(monkeypatch):
    import sys
    import types
    import unittest

    from tests.manual.named_probe_review_cases import cases
    fixtures=cases()
    assert len(fixtures)==4
    for case in fixtures:
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
        peer=next(c for c in fixtures if c.name.rsplit('-',1)[0]==case.name.rsplit('-',1)[0] and c.buggy!=case.buggy)
        assert peer.task==case.task and peer.files==case.files


def test_wrong_expected_value_is_not_validated_by_actual_return():
    from tests.manual.named_probe_review_cases import cases, score_probe
    case=next(c for c in cases() if c.name=='rightmost-match-bug')
    p={'args':[],'kwargs':{'values':[2,2],'target':2},'expected':0}
    score=score_probe(case,p,{'status':'returned','value':0})
    assert score['valid_domain'] and score['distinguishes']
    assert score['expected_correct'] is False


def test_unknown_generation_recovery_preserves_unknown_cost_and_never_replays(tmp_path):
    import json
    from types import SimpleNamespace

    from tests.manual.named_probe_experiment_recovery import recover_unknown_proposal
    from tests.manual.open_code_review_self_review_ab import digest
    case=SimpleNamespace(name='case',cohort='holdout',buggy=False,candidate=SOURCE)
    (tmp_path/'state').mkdir()
    (tmp_path/'state/discovery.json').write_text(json.dumps({'status':'plan_only','verdict':None,'recorded_plan':{
        'source_text_sha256':digest(SOURCE),'hypotheses':[],'protocol_usable':False,
        'errors':['GenerationUnknownError: generation outcome unknown; request not resent: timed out']}}))
    journal=tmp_path/'journal.jsonl'
    journal.write_text(json.dumps({'arm':'discovery/case/82/plan-native','path':'/v1/chat/completions',
                                   'request_id':'x','response_complete':False})+'\n')
    row=recover_unknown_proposal(case,82,tmp_path,journal)
    assert row['seconds'] is None and row['returncode'] is None
    assert row['qualified_patch'] is None and not row['success']
    assert row['usage']['usage_complete'] is False
    assert row['first_review']['verdict'] is None


def test_resume_identities_are_unique_and_do_not_forget_unknown_attempt():
    from tests.manual.named_probe_experiment_recovery import completed_keys
    row={'stage':'proposal','case':'case','seed':82,'arm':'plan-native','process_status':'unknown'}
    assert completed_keys([row])=={('proposal','case',82,'plan-native')}
    with pytest.raises(ValueError,match='duplicate'):
        completed_keys([row,row])


def test_data_only_probe_scoring_keeps_unexecuted_observation_unknown():
    from tests.manual.counterexample_review_quality_ab import score_evidence
    evidence=[{'hypotheses':[{'probe':{'args':[],'kwargs':{'value':1},'expected':1}}], 'actual':None}]
    scores=score_evidence(None,evidence)
    assert scores[0]['actual'] is None
    assert scores[0]['valid_domain'] is None and scores[0]['expected_correct'] is None
    assert scores[0]['distinguishes'] is False


def test_resume_only_schedules_predeclared_unfinished_final_stages():
    from tests.manual.named_probe_resume_quality_ab import pending_stages
    from tests.manual.named_probe_review_cases import cases
    selected=cases()
    jobs=pending_stages(selected,[81,82],[])
    assert len(jobs)==48
    case,seed,arm,stage=jobs[0]
    completed=[{'stage':stage,'case':case.name,'seed':seed,'arm':arm}]
    rest=pending_stages(selected,[81,82],completed)
    assert len(rest)==47 and jobs[0] not in rest
    assert all(t in ('semantic','repair') for _,_,_,t in rest)


def test_envelope_audit_removes_only_complete_outer_json_fence():
    from tests.manual.named_probe_text_envelope_audit import remove_json_envelope
    assert remove_json_envelope('```json\n{"probes": []}\n```')==('{"probes": []}',True)
    text='Some advice\n```json\n{"probes": []}\n```'
    assert remove_json_envelope(text)==(text,False)
