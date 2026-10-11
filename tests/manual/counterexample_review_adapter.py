"""Experiment-only bounded observations; proposed expectations are never oracles.

Execution is explicitly granted only for synthetic fixtures copied to an isolated
child. This is not a production tool or a natural-language oracle. The delegate
still judges semantics; hidden reference functions are used only offline.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def digest(source):
    return hashlib.sha256(source).hexdigest()


def validate_probe(probe, task):
    if not isinstance(probe, dict):
        raise ValueError('probe must be data')
    fields = {'requirement_quote', 'preconditions', 'args', 'kwargs', 'expected', 'alternative', 'suggested_fix'}
    if set(probe) != fields:
        raise ValueError('probe fields do not match the data contract')
    for key in ('requirement_quote', 'preconditions', 'alternative', 'suggested_fix'):
        if not isinstance(probe[key], str) or not 1 <= len(probe[key]) <= 1000:
            raise ValueError('probe text must be bounded')
    if probe['requirement_quote'] not in task:
        raise ValueError('requirement quote is absent from the original task')
    if not isinstance(probe['args'], list) or not isinstance(probe['kwargs'], dict):
        raise ValueError('only JSON positional and keyword arguments are allowed')
    if any(not isinstance(k, str) or not k.isidentifier() or k.startswith('_') for k in probe['kwargs']):
        raise ValueError('invalid keyword')
    if len(json.dumps(probe, allow_nan=False)) > 12000:
        raise ValueError('probe exceeds budget')
    return {'quote_present': True, 'semantic_admission': 'unverified'}


def online_manifest(case):
    return {'task': case.task, 'files': case.files, 'candidate': case.candidate}


def observe(project, probes, *, authorized=False):
    if not authorized:
        raise PermissionError('fixture execution was not explicitly authorized')
    if not isinstance(probes, list) or len(probes) > 2:
        raise ValueError('at most two probes')
    source = (project / 'app.py').read_bytes()
    if len(source) > 20000:
        raise ValueError('fixture too large')
    # Strip model expectations, explanations and fixes before invoking the child.
    spec = {'source': source.decode('utf-8'), 'probes': [
        {'args': p['args'], 'kwargs': p['kwargs']} for p in probes]}
    from codey.runtime.core.cancellation import terminate_process_tree
    from tests.manual.codey_vs_pi_agent_stability_ab import _spawn

    worker = Path(__file__).with_name('counterexample_observation_worker.py')
    with tempfile.TemporaryDirectory(prefix='codey-frozen-observation-') as directory:
        import os
        proc, owner = _spawn([sys.executable, '-B', '-I', str(worker)], Path(directory),
                            dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        try:
            stdout, stderr = proc.communicate(json.dumps(spec).encode(), timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc, owner)
            proc.communicate()
            result = {'observations': [{'status': 'timeout'} for _ in probes]}
        else:
            if proc.returncode or len(stdout) > 128000:
                result = {'observations': [{'status': 'worker_error', 'detail': stderr.decode('utf-8', 'replace')[-1000:]}
                                           for _ in probes]}
            else:
                result = json.loads(stdout)
        finally:
            owner.close()
    result.update({'source_sha256': digest(source), 'final_source_sha256': digest((project / 'app.py').read_bytes()),
                   'authority': 'explicit_experiment_fixture_grant', 'semantic_admission': 'unverified'})
    if result['source_sha256'] != result['final_source_sha256']:
        raise RuntimeError('candidate changed during observation')
    return result


PLAN_CONTRACT = '''Experiment method: propose at most TWO hypotheses about incorrect interpretations.
For each, quote the Original user task exactly, state valid-input preconditions,
and give JSON arguments distinguishing the interpretations. Do not invent a requirement.
An expectation is a proposal, not an oracle. No scripts, expressions, file paths, or tools.
Target only app.py's synchronous select function. Consider correctness, not style.
Return exactly JSON {"hypotheses":[]} if no concrete concern; otherwise
{"hypotheses":[{"requirement_quote":"exact task quote","preconditions":"domain",
"args":[],"kwargs":{},"expected":null,"alternative":"wrong interpretation",
"suggested_fix":"minimal recommendation"}]}. Keep each field concise.
This first reply is a probe plan, not a final Codey verdict.'''


class CounterexampleReviewer:
    def __init__(self, delegate, project, task, log, *, authorized=False):
        self.delegate, self.project, self.task, self.log = delegate, project, task, log
        self.authorized, self.started = authorized, False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def new_chat(self, *args, **kwargs):
        self.started = False
        return self.delegate.new_chat(*args, **kwargs)

    def send(self, prompt, timeout=None):
        if self.started:
            return self.delegate.send(prompt, timeout=timeout)
        self.started = True
        raw = self.delegate.send(prompt + '\n\n' + PLAN_CONTRACT, timeout=timeout)
        row = {'raw_plan': raw, 'hypotheses': [], 'invalid_probes': []}
        try:
            plan = json.loads(raw)
            probes = plan['hypotheses']
            if not isinstance(probes, list) or len(probes) > 2:
                raise ValueError('at most two hypotheses')
            valid = []
            for index, probe in enumerate(probes):
                try:
                    provenance = validate_probe(probe, self.task)
                except (ValueError, TypeError) as exc:
                    row['invalid_probes'].append({'index': index, 'probe': probe, 'error': str(exc)})
                else:
                    valid.append(probe)
                    row['hypotheses'].append({'probe': probe, **provenance})
            row['actual'] = observe(self.project, valid, authorized=self.authorized) if valid else None
        except (ValueError, KeyError, TypeError) as exc:
            row['plan_error'] = str(exc)
        # No admission bit or pass/fail is computed from proposed expected values.
        control = ('Probe observations follow. Quotes were checked ONLY for presence, not meaning; '
                   'valid domain and expected behavior are still your responsibility. A returned value '
                   'or exception alone does not establish a defect. Discard unsupported findings, '
                   'but an invalid probe does not prove the candidate correct. '
                   'Now submit the standard Codey review JSON specified in the original request. '
                   'For concrete findings include the valid input, actual observation, and requirement '
                   'basis in issue. Suggest a repair only if supported; no new commands.\n'
                   + json.dumps(row, ensure_ascii=False))
        try:
            answer = self.delegate.send(control, timeout=timeout)
            row['final_reply'] = answer
            return answer
        finally:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
