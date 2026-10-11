"""Experiment-only signature-guided proposals; no task-specific acceptance rules."""
import ast
import json

from tests.manual.counterexample_review_adapter import digest, observe


def parameter_shape(source):
    fn = next((n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'select'), None)
    if fn is None or fn.args.posonlyargs or fn.args.vararg or fn.args.kwarg:
        raise ValueError('only synchronous functions with named parameters supported')
    names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    required = [a.arg for a in fn.args.args[:len(fn.args.args)-len(fn.args.defaults)]]
    required += [a.arg for a, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True) if default is None]
    return names, required


def probe_schema(source):
    names, required = parameter_shape(source)
    fields = {k: {'type': 'string'} for k in ('requirement_quote', 'preconditions', 'alternative')}
    fields.update({'arguments': {'type': 'object', 'properties': {name: {} for name in names},
                                 'required': required, 'additionalProperties': False}, 'expected': {}})
    return {'type': 'object', 'properties': {'probes': {'type': 'array', 'maxItems': 2,
        'items': {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}},
        'required': ['probes'], 'additionalProperties': False}


def validate_plan(plan, source, task):
    names, required = parameter_shape(source)
    row = {'hypotheses': [], 'errors': [], 'source_text_sha256': digest(source.encode())}
    if not isinstance(plan, dict) or set(plan) != {'probes'} or not isinstance(plan['probes'], list) or len(plan['probes']) > 2:
        row['errors'].append('plan must contain at most two probes')
        return row
    fields = {'requirement_quote', 'preconditions', 'arguments', 'expected', 'alternative'}
    for index, probe in enumerate(plan['probes']):
        try:
            if not isinstance(probe, dict) or set(probe) != fields:
                raise ValueError('probe fields mismatch')
            args = probe['arguments']
            if not isinstance(args, dict) or set(args)-set(names) or set(required)-set(args):
                raise ValueError('named arguments do not bind the actual function signature')
            if not isinstance(probe['requirement_quote'], str) or not probe['requirement_quote'] or probe['requirement_quote'] not in task:
                raise ValueError('quote absent from original requirement')
            if any(not isinstance(probe[k], str) or len(probe[k]) > 1000 for k in ('preconditions', 'alternative')):
                raise ValueError('explanations must be bounded text')
            if len(json.dumps(probe, allow_nan=False)) > 12000:
                raise ValueError('probe too large')
            row['hypotheses'].append({'probe': {'requirement_quote': probe['requirement_quote'],
                'preconditions': probe['preconditions'], 'args': [], 'kwargs': args, 'expected': probe['expected'],
                'alternative': probe['alternative'], 'suggested_fix': 'Not proposed during input selection.'},
                'quote_present': True, 'signature_bound': True, 'semantic_admission': 'unverified'})
        except (ValueError, TypeError) as exc:
            row['errors'].append({'index': index, 'probe': probe, 'error': str(exc)})
    return row


def proposal_prompt(context, schema, *, native):
    # Remove the old verdict-format instructions; preserve the same task/diff data.
    body = context[context.index('Project: '):] if 'Project: ' in context else context
    return ('Investigate the submitted candidate against the Original user task, independently of Writer claims. '
            'Propose up to two concrete inputs distinguishing the actual implementation from a plausible '
            'requirement-compliant interpretation. Inputs must meet the stated domain; literal JSON values, '
            'not variable names or expressions. Quote an exact requirement, state its preconditions, '
            'and propose the expected return value. Expected values are hypotheses, not trusted truth. '
            'Do not invent constraints or request style-only changes. If no concrete concern, submit empty probes. '
            + ('Call submit_probe_plan exactly once. ' if native else 'Return exactly one JSON object. ')
            + 'Required data schema:\n' + json.dumps(schema) + '\n\n' + body)


def generate_plan(delegate, project, task, context, *, native, timeout=None):
    source = (project / 'app.py').read_text(encoding='utf-8')
    schema = probe_schema(source)
    prompt = proposal_prompt(context, schema, native=native)
    delegate.new_chat()
    try:
        if native:
            from codey.providers.base import ProviderToolDefinition, ProviderToolResult
            tools = [ProviderToolDefinition('submit_probe_plan', 'Submit concrete data hypotheses; does not execute code.', schema)]
            turn = delegate.send_turn(prompt, tools, timeout=timeout)
            calls = turn.tool_calls
            if len(calls) != 1 or calls[0].name != 'submit_probe_plan':
                raise ValueError('expected exactly one submit_probe_plan call')
            raw = calls[0].arguments
            delegate.acknowledge_tool_results([ProviderToolResult(calls[0].id, 'Proposal archived, not verified.')], tools)
        else:
            text = delegate.send(prompt, timeout=timeout)
            raw = json.loads(text)
        row = validate_plan(raw, source, task)
        row['raw_plan'] = raw
        row['protocol_usable'] = not row['errors']
    except Exception as exc:
        row = {'hypotheses': [], 'errors': [type(exc).__name__ + ': ' + str(exc)], 'protocol_usable': False,
               'source_text_sha256': digest(source.encode())}
    row['transport'] = 'native-tool' if native else 'plain-json'
    return row


class NamedProbeReviewer:
    def __init__(self, delegate, project, task, log, *, execute, prepared_plan=None):
        self.delegate, self.project, self.task, self.log = delegate, project, task, log
        self.execute, self.prepared_plan, self.started = execute, prepared_plan, False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def new_chat(self, *args, **kwargs):
        self.started = False
        return self.delegate.new_chat(*args, **kwargs)

    def send(self, prompt, timeout=None):
        if self.started:
            return self.delegate.send(prompt, timeout=timeout)
        self.started = True
        source = (self.project / 'app.py').read_text(encoding='utf-8')
        prepared = self.prepared_plan
        if prepared and prepared['source_text_sha256'] == digest(source.encode()):
            # Shared model-generated plan, not a fabricated model response or oracle.
            row = json.loads(json.dumps(prepared))
            row['plan_origin'] = 'shared_real_model_proposal'
        else:
            row = generate_plan(self.delegate, self.project, self.task, prompt, native=True, timeout=timeout)
            row['plan_origin'] = 'new_current_source_proposal'
        probes = [h['probe'] for h in row['hypotheses']]
        row['actual'] = observe(self.project, probes, authorized=True) if self.execute and probes else None
        row['observation_mode'] = 'executed' if self.execute else 'not_executed'
        control = ('Review data proposals below. Quote presence and argument binding are not semantic admission. '
                   'An observation is only what the candidate returned; domain and expected behavior still require '
                   'grounding in the Original user task. A bad or non-distinguishing probe does not imply approval. '
                   'Request changes only for a concrete supported defect. Do not request more scripts or shell commands. '
                   'Submit the standard review verdict/findings JSON requested above.\n' + json.dumps(row))
        self.delegate.new_chat()  # Identical fresh final-review context in both observation arms.
        try:
            answer = self.delegate.send(prompt + '\n\n' + control, timeout=timeout)
            row['final_reply'] = answer
            return answer
        finally:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
