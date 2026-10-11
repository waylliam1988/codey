"""Experiment-only OCR templates and read tools; not an OCR CLI reimplementation.

Templates/tool schemas are loaded from the specified Apache-2.0 reference tree.
Grouping, planning, comment relocation and reflection are deliberately not emulated.
"""
import json
import re
from pathlib import Path


def reference_prompt(context: str, reference: Path) -> str:
    root = reference / 'internal/config/template/prompts'
    system = (root / 'main_task_system.md').read_text(encoding='utf-8')
    user = (root / 'main_task_user.md').read_text(encoding='utf-8')
    rules = (reference / 'internal/config/rules/rule_docs/python.md').read_text(encoding='utf-8')
    body = context[context.index('Project: '):] if 'Project: ' in context else context
    background, _, diff = body.partition('Diff:\n')
    values = {'requirement_background': background, 'system_rule': rules, 'diffs': diff,
              'change_files': '', 'current_system_date_time': '2026-10-11',
              'plan_guidance': 'Review each changed file; inspect related callers when needed.',
              'confirmed_comments': '(none)'}
    user = re.sub(r'\{\{(\w+)\}\}', lambda m: values[m[1]], user)
    return (system + '\n' + user + '\nExperiment output contract: report only concrete correctness or '
            'security defects, not style preferences. Keep findings on changed paths. '
            'Read tools inspect frozen Python files; searches are literal and require exact paths. '
            'Only bug/security/test defects block completion; other categories are recorded as advice. '
            'You may finish through task_done or return Codey JSON: '
            '{"verdict":"approved","summary":"Looks good","findings":[]} or '
            '{"verdict":"changes_requested","summary":"Defect found","findings":'
            '[{"path":"app.py","issue":"Concrete defect","suggested_fix":"Fix"}]}.')


class ReadonlyWorkspace:
    def __init__(self, project: Path):
        root = project.resolve()
        self.files = {p.relative_to(project).as_posix(): p.read_text(encoding='utf-8')
                      for p in project.rglob('*.py') if not p.is_symlink() and p.resolve().is_relative_to(root)}
        if len(self.files) > 50 or sum(map(len, self.files.values())) > 100000:
            raise ValueError('review fixture exceeds the frozen workspace budget')

    def execute(self, name: str, args: dict) -> str:
        if name == 'file_read':
            path = args.get('file_path')
            if path not in self.files:
                raise ValueError('file is outside the frozen review workspace')
            start, end = args.get('start_line', 1), args.get('end_line', 200)
            if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start >= 200:
                raise ValueError('invalid bounded line range')
            return '\n'.join(f'{i}| {line}' for i, line in enumerate(self.files[path].splitlines(), 1)
                             if start <= i <= end)[:12000]
        if name == 'code_search':
            query = args.get('search_text')
            if not isinstance(query, str) or not 1 <= len(query) <= 200 or args.get('use_perl_regexp'):
                raise ValueError('only bounded literal search is available')
            patterns = args.get('file_patterns') or tuple(self.files)
            if not isinstance(patterns, (list, tuple)) or any(p not in self.files for p in patterns):
                raise ValueError('search requires exact frozen workspace paths')
            hits = [f'{path}:{i}| {line}' for path in patterns
                    for i, line in enumerate(self.files[path].splitlines(), 1)
                    if (query if args.get('case_sensitive') else query.lower()) in
                       (line if args.get('case_sensitive') else line.lower())]
            return '\n'.join(hits[:100])[:12000] or 'No matches.'
        raise ValueError('review tools cannot write or execute commands')


class OCRReviewer:
    """Bounded read-only candidate replacing only the reviewer provider in a child."""
    def __init__(self, delegate, project: Path, reference: Path, log: Path, max_rounds=4):
        self.delegate, self.project, self.reference = delegate, project, reference
        self.log, self.max_rounds = log, max_rounds
        self.started = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def new_chat(self, *args, **kwargs):
        self.started = False
        return self.delegate.new_chat(*args, **kwargs)

    def send(self, prompt, timeout=None):
        from codey.providers.base import ProviderToolDefinition, ProviderToolResult

        workspace = ReadonlyWorkspace(self.project)
        definitions = json.loads((self.reference / 'internal/config/toolsconfig/tools.json').read_text(encoding='utf-8'))
        tools = [ProviderToolDefinition(t['name'], t['definition']['description'], t['definition']['parameters'])
                 for t in definitions if t['name'] in {'task_done', 'code_comment', 'file_read', 'code_search'}]
        # The production service's one format attempt belongs to this review.
        # Do not replace it with a new task whose <review_files> is empty.
        request = prompt if self.started else reference_prompt(prompt, self.reference)
        self.started = True
        turn = self.delegate.send_turn(request, tools, timeout=timeout)
        findings = []
        invalid_finding = False
        for round_index in range(self.max_rounds):
            if not turn.tool_calls:
                if invalid_finding:
                    raise RuntimeError('OCR candidate contained an invalid finding; review incomplete')
                if findings:
                    raise RuntimeError('OCR candidate did not finish its recorded findings through task_done')
                return turn.text  # The existing production parser judges completeness.
            results, finish = [], None
            for call in turn.tool_calls:
                try:
                    if call.name == 'code_comment':
                        for comment in call.arguments['comments']:
                            path, snippet = comment['path'], comment['existing_code']
                            if path not in workspace.files or snippet.strip() not in workspace.files[path]:
                                raise ValueError('comment subject/snippet is not in the frozen workspace')
                            category = comment['category']
                            if category not in {'bug', 'security', 'test', 'performance', 'maintainability',
                                                'style', 'documentation', 'other'}:
                                raise ValueError('unknown comment category')
                            if category in {'bug', 'security', 'test'}:
                                findings.append({'path': path, 'issue': comment['content'],
                                                 'suggested_fix': comment.get('suggestion_code') or 'Correct the reported defect.'})
                        content = 'Comments recorded for the production review parser.'
                    elif call.name == 'task_done':
                        finish = call.arguments.get('state')
                        content = 'Review submitted.'
                    else:
                        content = workspace.execute(call.name, call.arguments)
                except (ValueError, KeyError, TypeError) as exc:
                    invalid_finding |= call.name == 'code_comment'
                    content = 'ERROR: ' + str(exc)
                with self.log.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps({'tool': call.name, 'args': call.arguments, 'result': content}) + '\n')
                results.append(ProviderToolResult(call.id, content))
            if finish is not None:
                self.delegate.acknowledge_tool_results(results, tools)
                if finish != 'DONE':
                    raise RuntimeError('OCR candidate declared incomplete review')
                if invalid_finding:
                    raise RuntimeError('OCR candidate contained an invalid finding; review incomplete')
                return json.dumps({'verdict': 'changes_requested' if findings else 'approved',
                                   'summary': 'Reference-guided review completed', 'findings': findings})
            if round_index + 1 < self.max_rounds:
                turn = self.delegate.send_tool_results(results, tools, timeout=timeout)
        raise RuntimeError('OCR candidate review-read budget exhausted; no approval synthesized')
