from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from codey.agents.consensus import (
    ConsensusAdvice,
    ConsensusResult,
    run_consensus as run_consensus_core,
    run_project_audit as run_project_audit_core,
)
from codey.agents.shell_approval import render_deferred_tool_calls
from codey.automation.browser_worker import submit as submit_browser_task
from codey.policies.limits import REVIEW_TIMEOUT, SHELL_OUTPUT_LIMIT, SHELL_TIMEOUT
from codey.policies.shell_followup import ShellFollowupInput, render_shell_followup
from codey.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_LABELS,
    borrow_open_provider,
    connect_existing_provider,
    connect_fresh_provider_tab,
    provider_tab_availability,
    warm_provider_tabs,
)
from codey.providers import controls as provider_controls
from codey.providers.capabilities import rank_providers
from codey.research.advisors import EvidencePack, run_research_advisors as run_research_advisors_core
from codey.reviews.core import ReviewResult, parse_review_with_repair, render_review_prompt
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime import cancellation
from codey.runtime.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt
from codey.storage.managed_outputs import ManagedOutputStore
from codey.utils.refs import clip, digest_text
from codey.utils.text_budget import clip_middle
from codey.workspace.setup_context import safe_setup_context
from codey.workspace.task_context import safe_verification_candidates


def reviewer_candidates(
    ctx: Any,
    writer_id: str,
    *,
    supervisor: object | None = None,
) -> tuple[str, ...]:
    writer = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
    if supervisor is None:
        supervisor = ctx.providers.supervisor
    candidates = tuple(
        provider_id
        for provider_id in PROVIDER_LABELS
        if provider_id != writer
        and provider_id != "local"
        and supervisor.is_available(provider_id)
    )
    return rank_providers(candidates, mode="review")


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_availability(ctx: Any) -> dict[str, bool]:
    return provider_availability_from_statuses(ctx, provider_tab_availability())


def provider_availability_from_statuses(
    ctx: Any,
    statuses: dict[str, bool],
) -> dict[str, bool]:
    supervisor = ctx.providers.supervisor
    return {
        provider_id: available and supervisor.is_available(provider_id)
        for provider_id, available in statuses.items()
    }


def provider_payload(statuses: dict[str, bool] | None = None) -> list[dict]:
    statuses = statuses or {}
    return [
        {"id": provider_id, "label": label, "available": bool(statuses.get(provider_id))}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_status_update(provider_id: str, available: bool) -> list[dict]:
    return [{
        "id": provider_id,
        "label": PROVIDER_LABELS.get(provider_id, provider_id),
        "available": available,
    }]


def run_provider_warmup(ctx: Any, runner=warm_provider_tabs) -> None:
    try:
        raw_statuses = runner()
        statuses = provider_availability_from_statuses(ctx, raw_statuses)
        ctx.emit({"type": "providers", "providers": provider_payload(statuses)})
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        try:
            ctx.emit({
                "type": "status",
                "status": "Provider warmup failed",
                "detail": clip(text, 240),
                "error_ref": digest_text(text)[:24],
            })
        except Exception:
            return


def start_provider_warmup(ctx: Any, runner=warm_provider_tabs) -> None:
    submit_browser_task(run_provider_warmup, ctx, runner)


def emit_review(ctx: Any, session_id: str, text: str) -> None:
    ctx.emit({"type": "review", "session_id": session_id, "text": text})


def run_review_attempt(
    ctx: Any,
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    change_brief: str,
    project_map: str,
    verification_map: str,
    review_impact_map: str,
    execution_evidence: str,
    reviewer_id: str,
    reviewer,
    self_review: bool,
    trace_recorder: object | None = None,
) -> tuple[str, ReviewResult]:
    try:
        reviewer.new_chat()
        prompt = render_review_prompt(
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
        )
        trace = FailOpenPromptTrace(trace_recorder)
        trace.call("record_permission_profile", "reviewer", phase="review")
        record_provider_send_prompt(
            trace_recorder,
            name="review_prompt",
            text=prompt,
            purpose="review prompt sent to provider",
            source_ref="provider_send:review",
            capability_id="review_runner",
        )
        with provider_controls.suppress_assistance():
            reply = reviewer.send(prompt, timeout=REVIEW_TIMEOUT)

            def send_repair_prompt(repair: str) -> str:
                record_provider_send_prompt(
                    trace_recorder,
                    name="review_repair_prompt",
                    text=repair,
                    purpose="review repair prompt sent to provider",
                    source_ref="provider_send:review_repair",
                    capability_id="review_runner",
                )
                return reviewer.send(repair, timeout=REVIEW_TIMEOUT)

            review = parse_review_with_repair(
                reply,
                send_repair_prompt,
                changes=changes,
            )
        label = review_label(reviewer_id)
        prefix = f"{label} self-review" if self_review else label
        if review.approved:
            emit_review(ctx, session_id, f"{prefix} approved")
        else:
            emit_review(ctx, session_id, f"{prefix} suggested changes")
        return reviewer_id, review
    finally:
        try:
            reviewer.close()
        except Exception:
            pass


def run_review(
    ctx: Any,
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    writer_id: str,
    change_brief: str = "",
    project_map: str = "",
    verification_map: str = "",
    review_impact_map: str | None = None,
    execution_evidence: str = "",
    trace_recorder: object | None = None,
) -> tuple[str, ReviewResult] | None:
    cancellation.check()
    last_error: Exception | None = None
    if review_impact_map is None:
        review_impact_map = safe_review_impact_map(project, changes)
    for reviewer_id in reviewer_candidates(ctx, writer_id):
        cancellation.check()
        try:
            reviewer = connect_existing_provider(reviewer_id)
            ctx.set_provider_session(reviewer_id, None)
            return run_review_attempt(
                ctx,
                session_id=session_id,
                project=project,
                task=task,
                writer_summary=writer_summary,
                changes=changes,
                recent_log=recent_log,
                change_brief=change_brief,
                project_map=project_map,
                verification_map=verification_map,
                review_impact_map=review_impact_map,
                execution_evidence=execution_evidence,
                reviewer_id=reviewer_id,
                reviewer=reviewer,
                self_review=False,
                trace_recorder=trace_recorder,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            last_error = exc
    cancellation.check()
    try:
        reviewer_id = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
        reviewer = connect_fresh_provider_tab(reviewer_id)
        return run_review_attempt(
            ctx,
            session_id=session_id,
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
            reviewer_id=reviewer_id,
            reviewer=reviewer,
            self_review=True,
            trace_recorder=trace_recorder,
        )
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


def connect_consensus_provider(selected_provider, provider_id: str):
    """Use an already-open sibling tab while a Writer provider is active."""

    if provider_id == "local":
        return connect_existing_provider(provider_id)
    owner_page = getattr(getattr(selected_provider, "session", None), "page", None)
    if owner_page is not None:
        helper = borrow_open_provider(provider_id, owner_page)
        if helper is None:
            raise RuntimeError(f"{review_label(provider_id)} tab is not open in this browser context")
        return helper
    return connect_existing_provider(provider_id)


def run_consensus(
    ctx: Any,
    *,
    selected_provider,
    selected_provider_id: str,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
    trace_recorder: object | None = None,
) -> ConsensusResult | None:
    return run_consensus_core(
        selected_provider=selected_provider,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        context=context,
        draft=draft,
        plan=plan,
        draft_first=draft_first,
        owner_prompt=owner_prompt,
        trace_recorder=trace_recorder,
    )


def run_project_audit(
    ctx: Any,
    *,
    project: str | Path,
    selected_provider=None,
    selected_provider_id: str,
    task: str,
    context: str = "",
    trace_recorder: object | None = None,
) -> tuple[ConsensusAdvice, ...]:
    return run_project_audit_core(
        project=project,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        context=context,
        trace_recorder=trace_recorder,
    )


def run_research_advisors(
    ctx: Any,
    *,
    selected_provider,
    selected_provider_id: str,
    pack: EvidencePack,
) -> tuple[ConsensusAdvice, ...]:
    return run_research_advisors_core(
        selected_provider_id=selected_provider_id,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        pack=pack,
    )


def safe_project_cwd(project: str | Path, rel: str) -> Path:
    root = Path(project).expanduser().resolve()
    cwd = (root / (rel or ".")).resolve()
    if root not in cwd.parents and cwd != root:
        raise ValueError("cwd escapes project root")
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    return cwd


def execute_approved_shell(
    ctx: Any,
    project: str | Path,
    rel: str,
    command: str,
    *,
    timeout: int | None = None,
    output_limit: int | None = None,
) -> dict:
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "command required", "exit_code": None, "output": ""}
    timeout = SHELL_TIMEOUT if timeout is None else timeout
    output_limit = SHELL_OUTPUT_LIMIT if output_limit is None else output_limit
    try:
        cwd = safe_project_cwd(project, rel)
        with cancellation.scope(ctx.run_registry.stop_flag):
            proc = cancellation.run_process(
                command,
                cwd=cwd,
                timeout=timeout,
                shell=True,
            )
    except cancellation.TaskCancelled:
        return {
            "ok": False,
            "error": "command stopped",
            "exit_code": None,
            "output": "",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"command timed out after {timeout}s",
            "exit_code": None,
            "output": "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "exit_code": None, "output": ""}

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    output, truncated = clip_middle(output, output_limit)
    return {
        "ok": True,
        "error": None,
        "exit_code": proc.returncode,
        "output": output,
        "truncated": truncated,
    }


def build_shell_approval_continuation(
    *,
    command: str,
    result: dict,
    post_approval_instructions: str = "",
    setup_context: str = "",
    followup_hints: str = "",
    deferred_tool_calls: tuple[dict[str, object], ...] = (),
) -> str:
    truncation_note = (
        "\nShell output was truncated. Do not assume omitted content "
        "is clean; inspect narrower output if needed.\n"
        if result.get("truncated")
        else ""
    )
    checklist = (post_approval_instructions or "").strip()
    checklist_block = f"{checklist}\n\n" if checklist else ""
    setup_block = f"{setup_context.strip()}\n\n" if setup_context.strip() else ""
    followup_block = f"{followup_hints.strip()}\n\n" if followup_hints.strip() else ""
    deferred = render_deferred_tool_calls(deferred_tool_calls)
    deferred_block = f"{deferred}\n\n" if deferred else ""
    return (
        "Continue the interrupted task in this same conversation.\n"
        "The user approved and ran this shell command:\n"
        f"{command}\n\n"
        f"Exit code: {result.get('exit_code')}\n"
        "Output:\n"
        f"{result.get('output') or result.get('error') or '(no output)'}\n\n"
        f"{truncation_note}"
        f"{setup_block}"
        f"{checklist_block}"
        f"{followup_block}"
        f"{deferred_block}"
        "Use this result to continue the original task. If the task is complete,"
        " reply with a JSON done tool call."
    )


def shell_continuation_setup_context(pending: dict) -> str:
    if pending.get("risk_label") not in {
        "dependency_install",
        "system_install",
        "external_source",
        "dev_server",
    }:
        return ""
    return safe_setup_context(pending["project"])


def shell_followup_verification_candidates(project: str | Path, risk_label: object):
    if risk_label not in {"dependency_install", "dev_server", "publish"}:
        return ()
    return safe_verification_candidates(project)


def shell_followup_hints(
    *,
    pending: dict,
    result: dict,
) -> str:
    return render_shell_followup(ShellFollowupInput(
        risk_label=str(pending.get("risk_label") or "generic"),
        exit_code=result.get("exit_code"),
        output=str(result.get("output") or result.get("error") or ""),
        truncated=bool(result.get("truncated")),
        verification_candidates=shell_followup_verification_candidates(
            pending["project"],
            pending.get("risk_label"),
        ),
    ))


__all__ = [
    "ManagedOutputStore",
    "build_shell_approval_continuation",
    "execute_approved_shell",
    "provider_availability",
    "provider_availability_from_statuses",
    "provider_payload",
    "provider_status_update",
    "review_label",
    "reviewer_candidates",
    "run_consensus",
    "run_project_audit",
    "run_provider_warmup",
    "run_research_advisors",
    "run_review",
    "run_review_attempt",
    "shell_continuation_setup_context",
    "shell_followup_hints",
    "shell_followup_verification_candidates",
    "start_provider_warmup",
]
