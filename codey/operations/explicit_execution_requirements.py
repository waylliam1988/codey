"""Narrow explicit execution requirements, projected from existing receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from codey.completion.contract import CompletionCheck
from codey.operations.kernel_provenance import _kernel_workspace_identity_of
from codey.runtime.core.models import ToolCall, ToolResult
from codey.workspace.revision import workspace_fingerprint


def completed_once_command(session: Any, call: ToolCall, *, project: Any,
                           ignored_paths: Iterable[str] = ()) -> ToolResult | None:
    command = call.args.get('command')
    if project is None or not isinstance(command, str) or not command:
        return None
    # Closed form from the original request, not a model-authored policy.
    task = session.task_text
    if not re.search(r'(?:^|[.!?]\s+)[Rr]un\s+`?' + re.escape(command) + r'`?\s+once\b', task):
        return None
    root = Path(project).resolve()
    cwd = (root / str(call.args.get('path') or '.')).resolve()
    if not cwd.is_relative_to(root):
        return None  # The normal path/permission guard handles invalid targets.
    fingerprint = workspace_fingerprint(root, ignored_paths=ignored_paths)
    if not fingerprint:
        return None
    for result_ref, result in session._memory_results.items():
        if result.call.name != 'run' or result.call.args.get('command') != command:
            continue
        previous_cwd = (root / str(result.call.args.get('path') or '.')).resolve()
        identity = _kernel_workspace_identity_of(result)
        if (previous_cwd != cwd or identity is None or identity.fingerprint != fingerprint
                or type(result.audit.get('exit_code')) is not int):
            continue
        return ToolResult(call, f'ERROR: This command already completed for the unchanged workspace. '
                          f'The original request permits it once. Read its saved output with '
                          f'read_tool_result using result_ref={result_ref}; no new execution occurred. '
                          f'After changing the workspace, verification can run again.', ok=False,
                          audit={'error_code': 'policy_denied'})
    return None


def _output_read_requirement(task: str) -> tuple[str, str] | None:
    match = re.search(r'(?:^|[.!?]\s+)[Rr]un\s+(`?)([^\n]{1,512}?)\1\s+once\b'
                      r'[^.!?\n]{0,160}?[,;]\s*then find\s+(`?)([A-Za-z_]\w{0,127})\3'
                      r'\s+in\s+(?:its|the)\s+saved output\b', task)
    if match is None:
        return None
    return match[2], match[4]


def pending_output_read(session: Any, task: str) -> tuple[str, str] | None:
    """A real owned excerpt satisfies the explicit read; previews/claims do not."""
    requirement = _output_read_requirement(task)
    if requirement is None:
        return None
    command, keyword = requirement
    references = {ref for ref, result in session._memory_results.items()
                  if result.call.name == 'run' and result.call.args.get('command') == command
                  and type(result.audit.get('exit_code')) is int
                  and _kernel_workspace_identity_of(result) is not None}
    for result in session._memory_results.values():
        if result.call.name != 'read_tool_result' or not result.ok:
            continue
        try:
            data = json.loads(result.model_text)
        except (ValueError, TypeError):
            continue
        if (isinstance(data, dict) and isinstance(data.get('result_ref'), str) and data['result_ref'] in references
                and isinstance(data.get('text'), str) and keyword in data['text']):
            return None
    ref = next((ref for ref in session._memory_results if ref in references), '')
    return keyword, ref


def requested_output_read_check(session: Any, task: str) -> CompletionCheck | None:
    if _output_read_requirement(task) is None:
        return None
    pending = pending_output_read(session, task)
    if pending is None:
        return CompletionCheck('requested_output_read', 'pass')
    return CompletionCheck('requested_output_read', 'not_run', 'requested_saved_keyword_unobserved')


def required_read_before_write(session: Any, call: ToolCall) -> ToolResult | None:
    pending = pending_output_read(session, session.task_text)
    if pending is None:
        return None
    keyword, ref = pending
    action = (f'read_tool_result with result_ref={ref} and query={keyword}' if ref
              else 'run the original requested command first')
    return ToolResult(call, f'ERROR: The original request requires reading {keyword} in the saved command output '
                      f'before implementing changes. Use {action}; a preview or guessed value is not that read. '
                      f'No file was changed.', ok=False, audit={'error_code': 'policy_denied'})


def readonly_fix_obstruction(session: Any) -> str | None:
    """Explain a stopped fix using current failed receipts, never model claims."""
    if (getattr(session, 'task_kind', '') != 'project' or not getattr(session, 'project', '')
            or getattr(session, 'edited_files', None)):
        return None
    policy = session.policy
    if policy.allows('project.write') or policy.allows('shell.approval'):
        return None
    task = session.task_text
    if not re.search(r'(?:^|[.!?]\s+)(?:The task requires (?:fixing|repairing)\b|'
                     r'(?:Fix|Repair|Implement)\b)', task):
        return None
    root = Path(session.project).resolve()
    fingerprint = workspace_fingerprint(root)
    if not fingerprint or fingerprint != session.workspace_fingerprint:
        return None
    latest = {(row.get('command'), row.get('cwd', '.')): row for row in session.verifications}
    for (command, cwd), row in latest.items():
        code = row.get('exit_code')
        if (row.get('passed') is not False or type(code) is not int or code <= 0
                or row.get('workspace_revision') != session.workspace_revision
                or row.get('workspace_fingerprint') != fingerprint):
            continue
        for result in session._memory_results.values():
            identity = _kernel_workspace_identity_of(result)
            if (result.call.name != 'run' or result.call.args.get('command') != command
                    or (root / str(result.call.args.get('path') or '.')).resolve() != (root / cwd).resolve()
                    or type(result.audit.get('exit_code')) is not int or result.audit['exit_code'] != code
                    or identity is None or identity.revision != session.workspace_revision
                    or identity.fingerprint != fingerprint):
                continue
            reason = (f'Current verification {command} failed with exit code {code}. '
                      f'The requested fix requires changes forbidden by the current write permissions; '
                      f'no fix was applied and completion is blocked.')
            if re.search(r'finish with JSON\b', task):
                return json.dumps({'status': 'blocked', 'reason': reason})
            return reason
    return None
