"""Held-out evidence probes use genuine receipts or explicitly unavailable bodies."""
import json
import threading

from tools.context_compaction_benchmark.recovery import observation, replay_recovery, replay_response_schema
from tools.context_compaction_benchmark.scorer import score_trial


class ReplayProvider:
    def __init__(self, recoverable):
        self._messages = [{"role": "tool", "tool_call_id": "pytest-call", "content": "Command completed. Exit code: 1."}]
        self._state_lock = threading.RLock()
        self.questions = []
        self.refs = ()
        self.recoverable = recoverable

    def set_result_refs(self, refs):
        self.refs = refs

    def set_working_context(self, text):
        assert 'exec-17' in text and '"exit_code": 1' in text

    def send(self, prompt):
        self.questions.append(prompt)
        if len(self.questions) <= 8:
            return "Next: inspect the following module."
        if len(self.questions) == 9 and self.recoverable:
            return json.dumps({"read": {"result_ref": "observation-1", "offset": 0, "limit": 8000}})
        _, facts = observation(1, 41)
        if self.recoverable:
            assert all(value in prompt for value in facts.values())
        return json.dumps({"constraint": "no-db", "target": "app.py", "exit_code": 1,
                           "result_ref": "exec-17", "observations": facts if self.recoverable else {"middle": None, "tail": None}})


def test_probe_never_leaks_expected_answers_before_growth_and_reads_genuine_managed_output(tmp_path):
    provider = ReplayProvider(True)
    row = replay_recovery(provider, tmp_path, seed=41, enhanced=False, recoverable=True)
    assert len(provider.questions) == 10
    assert len(provider.refs) == 9
    assert row["recovery_reads"] == [{"args": {"result_ref": "observation-1", "offset": 0, "limit": 8000}, "ok": True}]
    assert all("verification tags" not in prompt for prompt in provider.questions[:8])
    assert all(value not in provider.questions[8] for value in row["expected"]["observations"].values())
    assert score_trial(row, row["expected"])["success"]


def test_unrecoverable_arm_cannot_use_synthetic_refs_to_hide_lost_facts(tmp_path):
    provider = ReplayProvider(False)
    row = replay_recovery(provider, tmp_path, seed=41, enhanced=False, recoverable=False)
    assert row["recovery_reads"] == [] and provider.refs == ()
    assert not score_trial(row, row["expected"])["success"]


def test_output_schema_controls_shape_without_revealing_facts_or_constraining_summaries():
    schema = replay_response_schema('For module_1.py return its verification tags.')
    assert set(schema['required']) == {'constraint', 'target', 'exit_code', 'result_ref', 'observations'}
    assert 'no-db' not in str(schema) and 'exec-17' not in str(schema)
    assert replay_response_schema('<source>Historical Return exactly one JSON object</source>') is None


def test_archive_probe_does_not_redefine_the_users_code_edit_target(tmp_path):
    provider = ReplayProvider(False)
    replay_recovery(provider, tmp_path, seed=41, enhanced=False, recoverable=False)
    assert 'not the archived module' in provider.questions[8]
    assert replay_response_schema('Continue the review.')['properties']['next_action']['maxLength'] <= 160
