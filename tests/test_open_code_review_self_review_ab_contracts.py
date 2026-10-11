"""The OCR study must preserve reference evidence, bounds and blind controls."""
import pytest

from tests.manual.open_code_review_adapter import ReadonlyWorkspace, reference_prompt


@pytest.fixture
def reviewer_fixture(tmp_path):
    import json

    from codey.providers.base import AssistantTurn, ProviderToolCall
    from tests.manual.open_code_review_adapter import OCRReviewer

    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_text('a = 1\n')
    prompts = tmp_path / 'internal/config/template/prompts'
    prompts.mkdir(parents=True)
    for name in ('main_task_system.md', 'main_task_user.md'):
        (prompts / name).write_text('review')
    rules = tmp_path / 'internal/config/rules/rule_docs'
    rules.mkdir(parents=True)
    (rules / 'python.md').write_text('rules')
    schema = tmp_path / 'internal/config/toolsconfig'
    schema.mkdir(parents=True)
    (schema / 'tools.json').write_text(json.dumps([{'name': name, 'definition': {
        'description': name, 'parameters': {}}} for name in ('code_comment', 'task_done', 'file_read')]))

    class Delegate:
        next_calls = 0

        def __init__(self):
            self.prompts = []

        def send_turn(self, *args, **kw):
            self.prompts.append(args[0])
            return self.turn

        def send_tool_results(self, *args, **kw):
            self.next_calls += 1
            return AssistantTurn(text='{"verdict":"approved","summary":"ok","findings":[]}')

        def acknowledge_tool_results(self, *args, **kw):
            pass

    delegate = Delegate()
    return delegate, OCRReviewer(delegate, project, tmp_path, tmp_path / 'tools.log', max_rounds=1), AssistantTurn, ProviderToolCall


def test_read_budget_is_a_bound_on_physical_generations(reviewer_fixture):
    delegate, reviewer, turn, call = reviewer_fixture
    delegate.turn = turn(tool_calls=(call('1', 'file_read', {'file_path': 'app.py'}),))
    with pytest.raises(RuntimeError, match='budget'):
        reviewer.send('review')
    assert delegate.next_calls == 0


def test_invalid_finding_cannot_be_silently_converted_to_approval(reviewer_fixture):
    delegate, reviewer, turn, call = reviewer_fixture
    delegate.turn = turn(tool_calls=(call('1', 'code_comment', {'comments': [
        {'path': 'app.py', 'existing_code': 'missing snippet', 'content': 'defect'}]}),
        call('2', 'task_done', {'state': 'DONE'})))
    with pytest.raises(RuntimeError, match='invalid'):
        reviewer.send('review')


def test_failed_review_does_not_synthesize_approval(reviewer_fixture):
    delegate, reviewer, turn, call = reviewer_fixture
    delegate.turn = turn(tool_calls=(call('1', 'task_done', {'state': 'FAILED'}),))
    with pytest.raises(RuntimeError, match='incomplete'):
        reviewer.send('review')


def test_format_repair_does_not_become_a_new_empty_diff_review(reviewer_fixture):
    delegate, reviewer, turn, _ = reviewer_fixture
    delegate.turn = turn(text='{"verdict":"approved","summary":"ok","findings":[]}')
    reviewer.send('Project: fixture\nOriginal user task: preserve ids\nDiff:\n+return value')
    control = 'Return only JSON now, preserving the previous verdict and concrete findings.'
    reviewer.send(control)
    assert delegate.prompts[-1] == control


def test_comments_cannot_disappear_when_a_later_text_reply_approves(reviewer_fixture):
    delegate, reviewer, turn, call = reviewer_fixture
    reviewer.max_rounds = 2
    delegate.turn = turn(tool_calls=(call('1', 'code_comment', {'comments': [
        {'path': 'app.py', 'existing_code': 'a = 1', 'content': 'defect', 'category': 'bug'}]}),))
    with pytest.raises(RuntimeError, match='recorded findings'):
        reviewer.send('review')


@pytest.mark.parametrize('category,verdict', [('bug', 'changes_requested'), ('security', 'changes_requested'),
    ('performance', 'approved'), ('style', 'approved'), ('maintainability', 'approved')])
def test_ocr_advice_and_blocking_defects_keep_their_distinct_meanings(reviewer_fixture, category, verdict):
    import json

    delegate, reviewer, turn, call = reviewer_fixture
    delegate.turn = turn(tool_calls=(call('1', 'code_comment', {'comments': [
        {'path': 'app.py', 'existing_code': 'a = 1', 'content': 'comment', 'category': category,
         'severity': 'low'}]}), call('2', 'task_done', {'state': 'DONE'})))
    result = json.loads(reviewer.send('review'))
    assert result['verdict'] == verdict
    assert bool(result['findings']) == (verdict == 'changes_requested')


def test_reference_strategy_uses_actual_review_instructions_and_matching_python_rules(tmp_path):
    prompts = tmp_path / 'internal/config/template/prompts'
    prompts.mkdir(parents=True)
    (prompts / 'main_task_system.md').write_text('Distinct reference role')
    (prompts / 'main_task_user.md').write_text('{{requirement_background}}\n{{system_rule}}\n{{diffs}}')
    rules = tmp_path / 'internal/config/rules/rule_docs'
    rules.mkdir(parents=True)
    (rules / 'python.md').write_text('Distinct Python rule')
    rendered = reference_prompt('Project: fixture\nOriginal user task: keep order\nDiff:\n+return values', tmp_path)
    assert 'Distinct reference role' in rendered and 'Distinct Python rule' in rendered
    assert 'Original user task: keep order' in rendered
    assert '{{' not in rendered


def test_reference_checkout_must_have_the_actual_templates(tmp_path):
    with pytest.raises(FileNotFoundError):
        reference_prompt('review', tmp_path)


def test_read_tools_preserve_source_and_allow_unchanged_caller_context(tmp_path):
    (tmp_path / 'app.py').write_text('def accept(n): return n\n')
    (tmp_path / 'caller.py').write_text('assert n >= 0\naccept(n)\n')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    tools = ReadonlyWorkspace(tmp_path)
    assert '1| assert n >= 0' in tools.execute('file_read', {'file_path': 'caller.py'})
    assert 'caller.py:2|' in tools.execute('code_search', {'search_text': 'accept('})
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('name,args', [('write', {}), ('run', {}), ('file_read', {'file_path': '../outside.py'}),
    ('file_read', {'file_path': 'app.py', 'start_line': 0}),
    ('file_read', {'file_path': 'app.py', 'end_line': 300}),
    ('code_search', {'search_text': 'a', 'file_patterns': ['../outside.py']}),
    ('code_search', {'search_text': '.*', 'use_perl_regexp': True})])
def test_review_tools_do_not_widen_access_or_execute_generated_code(tmp_path, name, args):
    (tmp_path / 'app.py').write_text('a = 1\n')
    with pytest.raises(ValueError):
        ReadonlyWorkspace(tmp_path).execute(name, args)


def test_fixed_bug_and_correct_controls_pass_visible_tests_but_only_correct_passes_hidden_oracle(monkeypatch):
    import types
    import unittest

    from tests.manual.open_code_review_quality_cases import cases

    for case in cases():
        if case.candidate is None:
            continue
        module = types.ModuleType('app')
        exec(case.candidate, module.__dict__)
        monkeypatch.setitem(__import__('sys').modules, 'app', module)
        suite_module = types.ModuleType('visible')
        exec(case.files['test_app.py'], suite_module.__dict__)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromModule(suite_module).run(result)
        assert result.wasSuccessful(), case.name
        if case.buggy:
            with pytest.raises(AssertionError):
                exec(case.oracle, {'m': module})
        else:
            exec(case.oracle, {'m': module})


def test_worker_manifest_does_not_leak_hidden_oracle_or_expected_verdict():
    from tests.manual.open_code_review_quality_cases import cases
    from tests.manual.open_code_review_self_review_ab import worker_manifest

    case = cases()[0]
    manifest = worker_manifest(case, case.candidate)
    assert set(manifest) == {'task', 'files', 'candidate'}
    assert case.oracle not in str(manifest)


def test_unavailable_review_is_not_an_approval():
    from tests.manual.open_code_review_self_review_ab import first_review

    assert first_review([])['verdict'] is None
    assert first_review([{'error': 'provider failed'}])['status'] == 'unavailable'
    assert first_review([{'review': {'verdict': 'approved', 'status': 'incomplete'}}])['status'] == 'unavailable'


def test_native_candidate_selection_does_not_filter_out_semantic_defects():
    from tests.manual.open_code_review_self_review_ab import native_candidate_ready

    result = {'terminal': {'completed': True}, 'returncode': 0, 'process_status': 'completed',
              'verification': {'visible_passed': True, 'hidden_passed': False, 'scope_ok': True,
                               'agent_verification_fresh': True}}
    assert native_candidate_ready(result)


def test_all_review_cases_authorize_the_replayed_candidate_edit():
    from codey.task.entry_auth import derive_entry_auth
    from tests.manual.open_code_review_quality_cases import cases

    for case in cases():
        auth = derive_entry_auth({'task': case.task, 'intent': 'project'}, project='fixture')
        assert 'project.write' not in auth.denied_capabilities, case.name


def test_native_freeze_accepts_verified_writer_when_only_disabled_review_blocks_delivery():
    from tests.manual.open_code_review_self_review_ab import native_candidate_ready

    result = {'terminal': {'completed': False, 'reason': 'blocked',
              'event': {'summary': 'Current candidate requires a fresh approved review.'}},
              'returncode': 1, 'process_status': 'completed',
              'verification': {'visible_passed': True, 'hidden_passed': False, 'scope_ok': True,
                               'agent_verification_fresh': True}}
    assert native_candidate_ready(result)
    result['terminal']['reason'] = 'stopped'
    assert not native_candidate_ready(result)
    result['terminal']['reason'] = 'blocked'
    result['verification']['agent_verification_fresh'] = False
    assert not native_candidate_ready(result)
