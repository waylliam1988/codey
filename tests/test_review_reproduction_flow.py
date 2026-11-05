"""Real Review service: independent contexts, persistence, replay and stale guards."""
import json
from types import SimpleNamespace

import pytest

from codey.app.review_service import run_review_attempt
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.reviews.persistence import ReviewArtifactStore, load_review_artifact
from codey.workspace.changes import ChangeTracker
from codey.workspace.task_changes import collect_task_changes
from tests.test_review_reproduction import POLICY, proposal


class Reviewer:
    native_tools = True
    model_identity = 'fixture/reviewer'

    def __init__(self, *, failure=None, mutate=None):
        self.prompts, self.chats, self.closed = [], 0, 0
        self.failure, self.mutate = failure, mutate

    def new_chat(self):
        self.chats += 1

    def close(self):
        self.closed += 1

    def send_turn(self, prompt, tools, timeout, *, require_tool_call):
        assert require_tool_call is True
        self.prompts.append((tools[0].name, prompt))
        if self.failure:
            raise self.failure
        name = tools[0].name
        if name == 'submit_review_probes':
            value = {'probes': [proposal()]}
        else:
            if self.mutate:
                self.mutate()
            value = {'verdict': 'changes_requested', 'summary': 'Wrong return value.',
                'findings': [{'path': 'app.py', 'issue': 'Input 5 returns 0, required 5.', 'suggested_fix': 'Return x.'}]}
        return AssistantTurn(tool_calls=(ProviderToolCall(str(len(self.prompts)), name, value),))

    def acknowledge_tool_results(self, results, tools):
        assert results[0].call_id == str(len(self.prompts))

    def send(self, prompt, timeout):
        self.prompts.append(('static', prompt))
        return json.dumps({'verdict': 'approved', 'summary': 'No issues.', 'findings': []})


def setup(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('def identity(x): return -x\n', encoding='utf-8')
    (root / 'consumer.py').write_text('from app import identity\n', encoding='utf-8')
    tracker = ChangeTracker(root)
    tracker.capture_before('app.py')
    (root / 'app.py').write_text('def identity(x): return 0\n', encoding='utf-8')
    tracker.capture_after('app.py')
    return root, tracker


def attempt(tmp_path, root, tracker, reviewer, *, task='Return the input.', **kwargs):
    rows = []
    return run_review_attempt(SimpleNamespace(emit=rows.append, state_home=tmp_path / 'state'), session_id='s',
        project=str(root), task=task, writer_summary='WRITER_CONCLUSION_TOKEN',
        changes=collect_task_changes(root, tracker), recent_log='WRITER_LOG_TOKEN',
        change_brief='WRITER_PLAN_TOKEN', project_map='Relevant modules: app.py, consumer.py',
        verification_map='', review_impact_map='consumer.py imports identity', execution_evidence='',
        reviewer_id='local', reviewer=reviewer, self_review=True, run_id='r', **kwargs)[1]


def test_fresh_independent_chats_and_actual_persisted_reproduction(tmp_path):
    root, tracker = setup(tmp_path)
    reviewer = Reviewer()
    review = attempt(tmp_path, root, tracker, reviewer, parent_policy=POLICY)
    assert review.needs_writer_repair and reviewer.chats == 2 and reviewer.closed == 1
    assert [name for name, _ in reviewer.prompts] == ['submit_review_probes', 'submit_review_result']
    actual = '\n'.join(prompt for _, prompt in reviewer.prompts)
    assert all(token not in actual for token in ('WRITER_CONCLUSION_TOKEN', 'WRITER_LOG_TOKEN', 'WRITER_PLAN_TOKEN'))
    assert 'consumer.py imports identity' in actual
    assert review.reproduction[0]['observation_kind'] == 'assertion_failed'
    assert '0 != 5' in review.reproduction[0]['output']
    assert (root / 'app.py').read_text() == 'def identity(x): return 0\n'
    loaded = load_review_artifact(ReviewArtifactStore(tmp_path / 'state'), session_id='s', run_id='r', attempt_id=review.identity.attempt_id)
    assert loaded.reproduction == review.reproduction


@pytest.mark.parametrize('policy,task', [(None, 'Return the input.'),
    (TaskPolicy(grants=frozenset({'control', 'project.read'})), 'Return the input.')])
def test_no_parent_verification_authority_retains_static_review(tmp_path, policy, task):
    root, tracker = setup(tmp_path)
    reviewer = Reviewer()
    review = attempt(tmp_path, root, tracker, reviewer, parent_policy=policy)
    assert reviewer.chats == 1 and review.approved and not review.reproduction
    assert [name for name, _ in reviewer.prompts] == ['static']


def test_retry_review_replays_identical_script_on_new_candidate(tmp_path):
    root, tracker = setup(tmp_path)
    initial = attempt(tmp_path, root, tracker, Reviewer(), parent_policy=POLICY)
    (root / 'app.py').write_text('def identity(x): return x\n', encoding='utf-8')
    tracker.capture_after('app.py')
    reviewer = Reviewer()
    repeated = attempt(tmp_path, root, tracker, reviewer, parent_policy=POLICY, replay_probes=initial.reproduction)
    assert [name for name, _ in reviewer.prompts] == ['submit_review_result']
    assert repeated.reproduction[0]['script'] == initial.reproduction[0]['script']
    assert repeated.reproduction[0]['observation_kind'] == 'passed'
    assert repeated.reproduction[0]['candidate_sha256'] != initial.reproduction[0]['candidate_sha256']


def test_changes_to_unmodified_caller_invalidate_review(tmp_path):
    root, tracker = setup(tmp_path)
    reviewer = Reviewer(mutate=lambda: (root / 'consumer.py').write_text('CHANGED=1\n'))
    review = attempt(tmp_path, root, tracker, reviewer, parent_policy=POLICY)
    assert review.status == 'stale' and not review.approved


def test_unknown_proposal_send_is_not_retried_or_converted_to_approval(tmp_path):
    root, tracker = setup(tmp_path)
    reviewer = Reviewer(failure=ConnectionError('response lost'))
    with pytest.raises(ConnectionError):
        attempt(tmp_path, root, tracker, reviewer, parent_policy=POLICY)
    assert len(reviewer.prompts) == 1 and reviewer.closed == 1


def test_explicit_no_tests_intent_does_not_gain_execution_from_review(tmp_path):
    root, tracker = setup(tmp_path)
    reviewer = Reviewer()
    review = attempt(tmp_path, root, tracker, reviewer, parent_policy=POLICY,
                     task='Return the input. Do not run tests.')
    assert not review.reproduction
    assert [name for name, _ in reviewer.prompts] == ['static']


def test_non_native_reviewer_retains_existing_static_contract(tmp_path):
    root, tracker = setup(tmp_path)
    reviewer = Reviewer()
    reviewer.native_tools = False
    review = attempt(tmp_path, root, tracker, reviewer, parent_policy=POLICY)
    assert review.approved and not review.reproduction
    assert [name for name, _ in reviewer.prompts] == ['static']
