"""Execute admitted Python properties with the existing process/receipt runtime."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from codey.completion.behavioral_checks import BehavioralObservation, BehavioralPlan, admit_behavioral_plan
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core import cancellation
from codey.runtime.core.output_capture import CAPTURE_LIMIT_BYTES
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.revision import workspace_fingerprint

if TYPE_CHECKING:
    from codey.operations.project_completion_context import ProjectRun

MAX_SOURCE_BYTES = 512 * 1024
MAX_SOURCE_FILES = 64


def _sources(project: Path) -> tuple[Path, ...]:
    paths = tuple(sorted(project.glob('*.py')))
    if len(paths) > MAX_SOURCE_FILES or any(p.is_symlink() or p.stat().st_size > MAX_SOURCE_BYTES for p in paths):
        raise ValueError('source_limit')
    return paths


def _targets(paths: tuple[Path, ...]) -> tuple[tuple[str, str], ...]:
    targets = []
    for path in paths:
        tree = ast.parse(path.read_bytes(), filename=path.name)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.decorator_list:
                args = node.args
                if len(args.posonlyargs) + len(args.args) == 1 and not (
                    args.vararg or args.kwarg or args.kwonlyargs or args.defaults
                ):
                    targets.append((path.name, node.name))
    return tuple(targets)


def prepare_behavioral_plan(project: Path, task: str) -> BehavioralPlan | None:
    try:
        return admit_behavioral_plan(task, _targets(_sources(project)))
    except (OSError, SyntaxError, ValueError):
        return None


def prepare_behavioral_validation(ctx: ProjectRun) -> None:
    ctx.behavioral_plan = prepare_behavioral_plan(Path(ctx.project), ctx.request.task)
    plan = ctx.behavioral_plan
    if plan is not None:
        ctx.hooks.append_ledger(lambda ledger: ledger.append('behavioral_plan_admitted',
            plan_digest=plan.digest, task_digest=plan.task_digest, path=plan.path,
            function=plan.function, requirement_quote=plan.requirement_quote, pairs=plan.pairs))


def run_behavioral_probe(project: Path, task: str, plan: BehavioralPlan, *, policy: TaskPolicy,
                         store: ManagedOutputStore, session_id: str, run_id: str,
                         timeout: float = 5.0) -> BehavioralObservation:
    def missing(reason: str, fingerprint: str = '') -> BehavioralObservation:
        return BehavioralObservation(plan.digest, fingerprint, 'not_run', reason)

    if not policy.allows('project.read') or not policy.allows('project.verify'):
        return missing('permission_denied')
    if hashlib.sha256(task.encode()).hexdigest() != plan.task_digest:
        return missing('task_changed')
    if admit_behavioral_plan(task, ((plan.path, plan.function),)) != plan:
        return missing('plan_invalid')
    try:
        paths = _sources(project)
        if (plan.path, plan.function) not in _targets(paths):
            return missing('binding_invalid')
        fingerprint = workspace_fingerprint(project)
        with tempfile.TemporaryDirectory(prefix='codey-behavior-') as temporary:
            root = Path(temporary)
            for path in paths:
                shutil.copyfile(path, root / path.name)
            manifest = {'path': plan.path, 'function': plan.function, 'pairs': plan.pairs}
            (root / 'probe.json').write_text(json.dumps(manifest), encoding='utf-8')
            process = cancellation.run_process(
                [sys.executable, '-I', '-B', str(Path(__file__).with_name('behavioral_probe_worker.py')), str(root)],
                cwd=root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=timeout,
                capture_limit_bytes=CAPTURE_LIMIT_BYTES,
            )
            result_path = root / 'result.json'
            if process.returncode != 0 or not result_path.is_file() or result_path.stat().st_size > 64_000:
                return missing('probe_protocol_error', fingerprint)
            raw = result_path.read_text(encoding='utf-8')
            result = json.loads(raw)
            if result['status'] not in {'pass', 'fail', 'not_run'}:
                return missing('probe_protocol_error', fingerprint)
        if workspace_fingerprint(project) != fingerprint:
            return missing('workspace_changed', fingerprint)
        ref = store.write_tool_output(session_id=session_id, run_id=run_id, tool_id='behavioral:' + plan.digest[:16],
            permission_profile='coding_writer', tool_name='behavioral_verification', display_ref=plan.path,
            text=raw, command='behavioral_verification', cwd='.')
        if ref is None:
            return missing('receipt_unavailable', fingerprint)
        return BehavioralObservation(plan.digest, fingerprint, result['status'], result['reason'], ref.handle,
                                     str(result['summary'])[:1500])
    except subprocess.TimeoutExpired:
        return missing('timeout')
    except (OSError, ValueError, SyntaxError, KeyError, TypeError):
        return missing('probe_protocol_error')


def refresh_behavioral_observation(ctx: ProjectRun) -> None:
    """Called for every current candidate, including unchanged verification tasks."""
    from codey.agents.protocol import task_forbids_verification
    from codey.policies.task_policy import build_task_policy

    plan = ctx.behavioral_plan
    if plan is None or ctx.result is None or ctx.result.stop_reason != 'done':
        return
    store = ctx.deps.persistence.managed_outputs
    if store is None or task_forbids_verification(ctx.request.task):
        ctx.behavioral_observation = BehavioralObservation(plan.digest, '', 'not_run', 'verification_unavailable')
    else:
        ctx.behavioral_observation = run_behavioral_probe(Path(ctx.project), ctx.request.task, plan,
            policy=build_task_policy(ctx.request, task_kind='project'), store=store,
            session_id=ctx.request.session_id, run_id=ctx.frame.run_id)
    observation = ctx.behavioral_observation
    ctx.hooks.append_ledger(lambda ledger: ledger.append('behavioral_observed',
        plan_digest=observation.plan_digest, workspace_fingerprint=observation.workspace_fingerprint,
        status=observation.status, reason=observation.reason, output_ref=observation.output_ref))


def behavioral_review_facts(ctx: ProjectRun) -> str:
    plan, observation = ctx.behavioral_plan, ctx.behavioral_observation
    if plan is None or observation is None:
        return ''
    return ('\nBehavioral verification (runtime observation, not writer claims):\n'
            + json.dumps({'requirement': plan.requirement_quote, 'plan_digest': plan.digest,
                          'workspace_fingerprint': observation.workspace_fingerprint,
                          'status': observation.status, 'reason': observation.reason,
                          'result_ref': observation.output_ref, 'observation': observation.summary}, ensure_ascii=False))
