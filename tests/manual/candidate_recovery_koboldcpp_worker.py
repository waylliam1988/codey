"""Local native continuation with explicitly labeled historical boundary replay."""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path


class NativeSteps:
    """Recorded historical tool prefix, never a model response."""
    name = 'recorded boundary tools'
    native_tools = True

    def __init__(self, steps):
        self.steps = list(steps)
        self.sequence = 0

    def new_chat(self, timeout=None):
        pass

    def close(self):
        pass

    def _next(self):
        from codey.providers.base import AssistantTurn, ProviderToolCall
        self.sequence += 1
        if not self.steps:
            return AssistantTurn(text='', tool_calls=())
        name, args = self.steps.pop(0)
        return AssistantTurn(text='', tool_calls=(ProviderToolCall(
            id=f'boundary-{self.sequence}', name=name, arguments=args),))

    def send_turn(self, prompt, tools, timeout=None):
        return self._next()

    def send_tool_results(self, results, tools, timeout=None):
        return self._next() if tools else self.acknowledge_tool_results(results, tools)

    def acknowledge_tool_results(self, results, declared_tools, timeout=None):
        from codey.providers.base import AssistantTurn
        return AssistantTurn(text='', tool_calls=())


def replay_steps(kind):
    from tests.manual.agent_stability_cases import COMMAND, CORRECT_APP, FIXTURE_FILES
    initial = FIXTURE_FILES['app.py']
    bad = ("import string\ndef normalize_name(value):\n"
           "    cleaned = ''.join(' ' if c in string.punctuation else c for c in value.lower())\n"
           "    return '-'.join(cleaned.split())\n")
    source = CORRECT_APP if kind == 'stale-search-replay' else bad
    replacement = {'path': 'app.py', 'replacements': [{'old_string': initial, 'new_string': source}]}
    steps = [('run', {'command': COMMAND, 'path': '.'}), ('read_file', {'path': 'app.py'}),
             ('read_file', {'path': 'test_app.py'}), ('edit', replacement)]
    if kind == 'stale-search-replay':
        steps += [('edit', replacement)] * 3
    elif kind == 'counterexample-repair-replay':
        steps += [('run', {'command': COMMAND, 'path': '.'}), ('done', {'summary': 'Recorded visible tests passed.'})]
    else:
        raise ValueError('Unknown replay prefix')
    return NativeSteps(steps)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--state-home', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--max-turns', type=int, default=30)
    parser.add_argument('task')
    args = parser.parse_args()
    variant = os.environ['CANDIDATE_AB_VARIANT']
    boundary = os.environ.get('CANDIDATE_AB_BOUNDARY', '')
    if variant not in {'current', 'proposal', 'production'}:
        raise ValueError('Unknown candidate experiment variant')
    home = str(args.state_home.resolve())
    os.environ.update({'HOME': home, 'USERPROFILE': home})
    if os.name == 'nt':
        os.environ.update({'HOMEDRIVE': Path(home).drive, 'HOMEPATH': home[len(Path(home).drive):]})

    from codey.agents.request import AgentRequest
    from codey.app import headless_runner
    from codey.operations.project_adapter import run
    from tests.manual.agent_stability_codey_worker import ControlledContext, sampling_provider
    patches = None
    if variant == 'proposal':
        from pytest import MonkeyPatch

        from tests.manual.candidate_recovery_test_proposal import install
        patches = MonkeyPatch()
        install(patches)
    attempts = 0

    def writer(request: AgentRequest):
        nonlocal attempts
        attempts += 1
        if boundary and attempts == 1:
            request = replace(request, provider=replay_steps(boundary), fresh_chat=True)
        return run(request)

    if boundary == 'counterexample-repair-replay':
        from codey.reviews.core import ReviewResult
        from codey.reviews.identity import ReviewIdentity, capture_snapshot
        original_build = headless_runner.build_task_deps
        reviews = 0

        def build(*a, **kw):
            deps = original_build(*a, **kw)
            native_review = deps.run_review

            def review(**kwargs):
                nonlocal reviews
                reviews += 1
                if reviews != 1:
                    return native_review(**kwargs)
                snapshot = capture_snapshot(args.project, ('app.py',))
                identity = ReviewIdentity('scope', 'recorded false approval', 'snapshot', str(args.project),
                    'historical-replay', '', 1, 'reviewer', False, snapshot_root=snapshot.root,
                    snapshot_files=snapshot.files, snapshot_inventory_digest=snapshot.inventory_digest)
                return 'historical-replay', ReviewResult('approved', 'Replayed historical false approval', [], identity=identity)

            return replace(deps, run_review=review)

        headless_runner.build_task_deps = build
    headless_runner.HeadlessAppContext = ControlledContext

    def emit(payload):
        headless_runner.emit_jsonl(payload)
        sys.stdout.flush()

    emit({'type': 'experiment_control', 'variant': variant, 'boundary': boundary,
          'first_writer': 'recorded tools' if boundary else 'native Local model',
          'first_review': 'recorded false approval' if boundary == 'counterexample-repair-replay' else 'native Local model',
          'remaining_writer_and_review': 'native Local model'})
    request = headless_runner.HeadlessRequest(project=args.project, task=args.task, provider_id='local',
        state_home=args.state_home, session_id=args.session_id, max_turns=args.max_turns)
    try:
        return headless_runner.run_headless(request, agent_run=writer, emit_jsonl=emit,
            connect_provider=sampling_provider, connect_reviewer=sampling_provider).exit_code
    finally:
        if patches is not None:
            patches.undo()


if __name__ == '__main__':
    raise SystemExit(main())
