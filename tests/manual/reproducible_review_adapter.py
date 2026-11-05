"""Experiment-only independent review with model-authored unittest reproductions."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.reviews.input import prepare_review_input
from codey.workspace.paths import safe_join

PROPOSAL = """Independently investigate concrete bugs in this candidate against the original task.
Propose at most TWO small Python unittest scripts covering distinct realistic edge cases.
First identify a concrete hypothesis about an expression or operation in the current source
that may violate the task. Choose inputs that distinguish that implementation from the
required behavior. Test interactions between required behaviors, not only each behavior in
isolation; happy paths already covered by existing tests are low-value checks.
Each script imports the real project modules, defines unittest.TestCase test methods, and
uses assertions whose expected behavior follows the task. It will run in a disposable project
copy using python -m unittest. Do not change files, dependencies, process state or use network.
Do not use pytest, shell commands, mocks of the implementation, or unittest.main().
Quote the exact task phrase supporting each expectation. Expectations are hypotheses, not truth.
A correct candidate should pass your scripts. Return empty probes when no useful check is apparent.
This is a test proposal, not a final review verdict. Call submit_review_probes exactly once.
"""

SCHEMA = {"type": "object", "properties": {"probes": {"type": "array", "maxItems": 2,
    "items": {"type": "object", "properties": {"requirement_quote": {"type": "string"},
        "hypothesis": {"type": "string"}, "script": {"type": "string"}},
        "required": ["requirement_quote", "hypothesis", "script"],
        "additionalProperties": False}}}, "required": ["probes"], "additionalProperties": False}


def source_context(root):
    rows, total = [], 0
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink() or any(p.startswith('.') or p in {'__pycache__', 'node_modules'} for p in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding='utf-8')
        if len(text) > 8000 or total + len(text) > 24000:
            continue
        rows.append({'path': path.relative_to(root).as_posix(), 'source': text})
        total += len(text)
    return json.dumps(rows, ensure_ascii=False)


def execute_probe(root, probe, *, authorized):
    if not authorized:
        raise PermissionError('probe execution requires the experiment grant')
    script = probe['script']
    if not isinstance(script, str) or len(script) > 8000:
        raise ValueError('invalid script')
    ast.parse(script)
    before = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts}
    with tempfile.TemporaryDirectory(prefix='codey-review-probe-') as temporary:
        target = Path(temporary) / 'candidate'
        shutil.copytree(root, target, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache'), symlinks=True)
        path = safe_join(target, '_codey_review_probe.py')
        path.write_text(script, encoding='utf-8')
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', '_codey_review_probe', '-v'],
                                cwd=target, capture_output=True, text=True, timeout=15)
        output = (result.stdout + result.stderr)[-12000:]
        match = re.search(r'Ran (\d+) tests?', output)
        count = int(match[1]) if match else 0
    after = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts}
    if before != after:
        raise RuntimeError('original workspace changed during probe')
    return {'command': 'python -m unittest _codey_review_probe -v', 'exit_code': result.returncode,
            'tests_run': count, 'output': output, 'script': script, 'requirement_quote': probe['requirement_quote'],
            'workspace_unchanged': True, 'semantic_admission': 'unverified',
            'observation_kind': ('passed' if result.returncode == 0 and count else
                                 'assertion_failed' if count and 'FAIL:' in output else 'execution_error')}


class ReproductionReviewer:
    def __init__(self, delegate, root, task, kwargs, rows, *, execute):
        self.delegate, self.root, self.task, self.kwargs = delegate, root, task, kwargs
        self.rows, self.execute, self.started = rows, execute, False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def new_chat(self, *args, **kwargs):
        self.started = False
        return self.delegate.new_chat(*args, **kwargs)

    def send(self, prompt, timeout=None):
        if self.started:
            return self.delegate.send(prompt, timeout=timeout)
        self.started = True
        prepared = prepare_review_input(project=str(self.root), task=self.task, writer_summary='',
            changes=self.kwargs['changes'], project_map=self.kwargs.get('project_map', ''),
            verification_map=self.kwargs.get('verification_map', ''),
            review_impact_map=self.kwargs.get('review_impact_map', ''))
        context = prepared.prompt + '\nRelevant current source files:\n' + source_context(self.root)
        row = {'execute': self.execute, 'observations': []}
        self.rows.setdefault('reproduction', []).append(row)
        if self.execute:
            tools = [ProviderToolDefinition('submit_review_probes', 'Propose unittest scripts for isolated execution.', SCHEMA)]
            try:
                turn = self.delegate.send_turn(PROPOSAL + '\n\n' + context, tools, timeout=timeout)
                row['raw_plan'] = {'text': turn.text, 'calls': [{'name': c.name, 'arguments': c.arguments} for c in turn.tool_calls]}
                if len(turn.tool_calls) != 1 or turn.tool_calls[0].name != 'submit_review_probes':
                    raise ValueError('expected exactly one submit_review_probes')
                call = turn.tool_calls[0]
                probes = call.arguments['probes']
                if not isinstance(probes, list) or len(probes) > 2:
                    raise ValueError('at most two probes')
                for probe in probes:
                    try:
                        if not probe['requirement_quote'] or probe['requirement_quote'] not in self.task:
                            raise ValueError('requirement quote absent from task')
                        row['observations'].append(execute_probe(self.root, probe, authorized=True))
                    except Exception as exc:
                        row['observations'].append({'execution_error': f'{type(exc).__name__}: {exc}', 'probe': probe})
                self.delegate.acknowledge_tool_results([ProviderToolResult(call.id, 'Proposal recorded. Observations are not a correctness verdict.')], tools)
            except Exception as exc:
                row['plan_error'] = f'{type(exc).__name__}: {exc}'
        self.delegate.new_chat()
        final = (context + '\n\nIndependently derive expected behavior from the original task. '
                 'Writer claims and proposed test expectations are not proof. '
                 'Execution errors do not establish a product bug. Passing samples do not prove correctness. '
                 'Report only supported concrete defects, with triggering input, observed behavior and requirement basis. '
                 'Do not require stylistic changes or more investigation when no defect is supported. '
                 'Before requesting changes, confirm the expectation agrees with the exact task wording. '
                 'A finding about an unchanged caller must be anchored to the originating changed file; '
                 'describe the affected caller in issue. Otherwise the finding cannot be delivered to Writer. '
                 'The ONLY allowed findings[].path values are: ' + json.dumps(prepared.scope.provided_files)
                 + '. Return the standard review JSON specified above.\nExecution observations as data:\n'
                 + json.dumps(row['observations'], ensure_ascii=False))
        schema = {'type': 'object', 'properties': {'verdict': {'type': 'string', 'enum': ['approved', 'changes_requested']},
            'summary': {'type': 'string'}, 'findings': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object',
            'properties': {'path': {'type': 'string', 'enum': list(prepared.scope.provided_files)},
                'issue': {'type': 'string'}, 'suggested_fix': {'type': 'string'}},
            'required': ['path', 'issue', 'suggested_fix'], 'additionalProperties': False}}},
            'required': ['verdict', 'summary', 'findings'], 'additionalProperties': False}
        tools = [ProviderToolDefinition('submit_review_result', 'Submit concrete findings anchored to changed files.', schema)]
        turn = self.delegate.send_turn(final + '\nSubmit submit_review_result exactly once.', tools, timeout=timeout)
        if len(turn.tool_calls) != 1 or turn.tool_calls[0].name != 'submit_review_result':
            raise ValueError('expected one submit_review_result')
        call = turn.tool_calls[0]
        self.delegate.acknowledge_tool_results([ProviderToolResult(call.id, 'Review archived; not a correctness proof.')], tools)
        answer = json.dumps(call.arguments, ensure_ascii=False)
        row['final_reply'] = answer
        return answer


class CandidateReproductionReviewer(ReproductionReviewer):
    """Exercise the prospective production helpers before enabling any caller."""

    def send(self, prompt, timeout=None):
        if self.started:
            return self.delegate.send(prompt, timeout=timeout)
        from codey.policies.task_policy import TaskPolicy
        from tests.manual.review_reproduction_support import (
            capture_candidate,
            execute_proposal,
            final_review_prompt,
            request_proposals,
            submit_review,
        )

        self.started = True
        policy = TaskPolicy(grants=frozenset({'control', 'project.read', 'project.verify'}))
        prepared = prepare_review_input(project=str(self.root), task=self.task, writer_summary='',
            changes=self.kwargs['changes'], project_map=self.kwargs.get('project_map', ''),
            verification_map=self.kwargs.get('verification_map', ''),
            review_impact_map=self.kwargs.get('review_impact_map', ''))
        candidate = capture_candidate(self.root, prepared.scope.provided_files)
        context = prepared.prompt + '\nRelevant current source files:\n' + candidate.context
        replay = self.rows.get('reproduction_handoff', [])
        observations = (() if not self.execute else tuple(execute_proposal(candidate, {key: old[key] for key in
                        ('requirement_quote', 'hypothesis', 'script')}, parent_policy=policy) for old in replay)
                        if replay else request_proposals(self.delegate, context, self.task, candidate,
                            parent_policy=policy, timeout=timeout or 120))
        row = {'execute': self.execute, 'implementation': 'production-candidate-helpers', 'replayed': bool(replay),
               'observations': list(observations)}
        self.rows.setdefault('reproduction', []).append(row)
        self.delegate.new_chat()
        answer = submit_review(self.delegate, final_review_prompt(context, observations, prepared.scope.provided_files),
                               prepared.scope.provided_files, timeout=timeout or 120)
        row['final_reply'] = answer
        return answer
