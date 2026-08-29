"""Task orchestration independent of HTTP request handling."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from codey.runtime import cancellation
from codey.providers import controls as provider_controls, flow as provider_flow
from codey.policies.action import ActionSubject, evaluate_action
from codey.policies.capability_registry import CapabilityRegistry
from codey.agents.runner import RunResult, task_forbids_verification
from codey.agents.tools import AgentToolFns
from codey.workspace.change_brief import (
    ChangeBrief,
    new_project_change_brief,
    project_audit_change_brief,
)
from codey.agents.consensus import (
    render_project_context,
)
from codey.completion.contract import (
    completion_proof_trace_payload,
)
from codey.completion.decision import (
    CompletionDecision,
    build_completion_decision,
    completion_blocked_reason,
)
from codey.completion.edit_integrity import (
    EditIntegrityObservation,
    observe_edit_integrity,
)
from codey.completion.edit_scope import changed_paths_from_changes
from codey.completion.repair_context import (
    RepairContextProjection,
    project_repair_context,
    repair_candidate,
)
from codey.completion.verification import (
    STANCE_FRESH_PASS,
    STANCE_INHERITED_PASS,
    decisive_failure_fact,
)
from codey.runtime.events import RunEvent, render_run_event, run_event_ui_payload
from codey.runtime.execution_evidence import ExecutionEvidence
from codey.ghost.continuity import build_ghost_continuity
from codey.ghost.directive import build_ghost_directive
from codey.ghost.learning_loop import (
    DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
    DEFAULT_GHOST_LEARNING_TIMEOUT,
    GhostLearningLoop,
    GhostLearningTurn,
)
from codey.ghost.router import (
    DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
    DEFAULT_GHOST_ROUTER_TIMEOUT,
    GhostRouteRequest,
    GhostRouteResult,
    GhostRouter,
)
from codey.ghost.work_queue import (
    GhostWorkItem,
    is_strict_work_continuation,
    proof_refs_from_task_event,
)
from codey.agents.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_continuation_prompt,
    render_handoff,
    render_recovered_handoff,
)
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.research_interest import (
    apply_research_affinity_hints,
    build_research_interest_candidates,
    candidate_to_topic_hint,
)
from codey.policies.permissions import allows_context_source, profile_for_name
from codey.knowledge.store import KnowledgeStore
from codey.knowledge.brief import KnowledgeBriefBuilder
from codey.storage.managed_outputs import (
    ManagedOutputStore,
    run_command_with_managed_output,
)
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.config import (
    ProjectConfigLoadResult,
    load_project_config,
    preferred_provider_for,
)
from codey.workspace.task_context import (
    ProjectTaskContextBuilder,
    safe_project_map,
    safe_verification_candidates,
)
from codey.providers import PROVIDER_LABELS
from codey.providers.capabilities import rank_providers
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.providers.supervisor import run_half_open_canary
from codey.runtime.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelopeSection,
    record_provider_send_prompt,
)
from codey.runs.receipt import VERIFICATION_TRUST_TRUSTED, build_task_receipt
from codey.runs.ledger import RunLedgerStore, RunLedgerWriter
from codey.runs.ledger_projection import (
    build_task_receipt_from_projection,
    load_run_projection,
)
from codey.runs.trace import (
    MAX_ANALYSIS_RUNS,
    MAX_ARTIFACT_REFS,
    RunTraceStore,
)
from codey.research.analysis_run import analysis_run_record
from codey.research.artifact_lineage import artifact_ref_from_managed_output
from codey.research.completion_gate import RESEARCH_QUEUE_KINDS, ResearchCompletionGate
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.evidence_followup import run_evidence_followup
from codey.research.evidence_ledger import EvidenceLedgerStore, EvidenceLedgerWriteResult
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline, ResearchPipelineConfig
from codey.research.proof_quality import proof_review_trace_payload
from codey.research.query_planner import build_research_plan, research_plan_trace_payload
from codey.research.reproducibility import build_reproducibility_capsule
from codey.research.topic_continuity import (
    CONTEXT_SOURCE_KEY as TOPIC_CONTINUITY_CONTEXT_SOURCE_KEY,
    MAX_TOPIC_CLAIM_REFS,
    project_topic_continuity,
)
from codey.research.browser_search import BrowserSearchProvider
from codey.research.runner import ResearchRunner
from codey.reviews.core import has_reviewable_changes
from codey.reviews.coordinator import ReviewCoordinator, change_state
from codey.reviews.impact_map import safe_review_impact_map
from codey.policies.shell_risk import classify_shell_risk
from codey.completion.verification_map import render_verification_map
from codey.completion.verification_policy import (
    check_covers_selected_candidate,
    select_verification_candidate,
    selected_verification_candidate_lines,
    verification_candidate_lines,
)
from codey.runs.work_checkpoint import (
    WorkCheckpoint,
    WorkCheckpointStore,
)
from codey.run_operation import (
    PHASE_COMPLETION_PROOF_RECORDED,
    RunOperationState,
    RunOperationStore,
    mark_completion_blocked,
    mark_completion_proof_recorded,
    mark_repair_context_admitted,
    mark_repair_running,
    mark_repair_settled,
    mark_terminal,
    mark_writer_running,
    mark_writer_settled,
)
from codey.agents.writer_failover import (
    CheckpointView,
    WriterAttempt,
    WriterFailoverRunner,
)


PRODUCTION_GHOST_ROUTER_TIMEOUT = 12.0
PRODUCTION_GHOST_ROUTER_NEW_CHAT_TIMEOUT = 8.0
PRODUCTION_GHOST_ROUTER_ATTEMPTS = 1


@dataclass(frozen=True)
class TaskRequest:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    intent: str = "auto"
    run_id: str = ""


@dataclass
class _RunFrame:
    request: TaskRequest
    run_id: str
    task_kind: str
    provider: Any | None
    provider_id: str
    project_text: str
    conversation: ConversationContext
    fresh_chat: bool
    handoff: str
    research_handoff: str
    prior_snapshot: ConversationSnapshot
    recovered_owner_prompt: str
    provider_session_changed: bool
    preflight_tried: set[str]
    preflight_switches: int
    trace: Any | None = None


@dataclass
class _RunWork:
    recent_events: list[str]
    evidence: ExecutionEvidence
    work_checkpoint: WorkCheckpoint | None = None
    ledger: RunLedgerWriter | None = None
    trace: Any | None = None
    record_agent_events_in_ledger: bool = False
    claimed_work_item: GhostWorkItem | None = None
    analysis_run_payloads: list[dict[str, object]] = field(default_factory=list)
    artifact_payloads: list[dict[str, object]] = field(default_factory=list)
    operation: RunOperationState | None = None


@dataclass(frozen=True)
class _RunHooks:
    on_event: Callable[[RunEvent], None]
    on_shell_request: Callable[[str, str], None]
    update_checkpoint: Callable[
        [Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint]],
        None,
    ]
    record_provider_failure: Callable[[str, ProviderFailure], None]
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None]
    provider_failover_order: Callable[[], tuple[str, ...]]
    supervisor: Any | None
    trace: Any | None = None


@dataclass(frozen=True)
class _ModeOutcome:
    event: dict
    research_result: Any | None = None
    research_pipeline_result: Any | None = None


def _prepend_ghost_directive(prompt: str, directive: str) -> str:
    text = str(directive or "").strip()
    if not text:
        return prompt
    return f"{text}\n\n{prompt}"


def _owner_prompt_with_ghost_directive(owner_prompt: str, directive: str) -> str:
    text = str(directive or "").strip()
    existing = str(owner_prompt or "").strip()
    if text and existing:
        return f"{text}\n\n{existing}"
    return text or existing


def _join_local_contexts(*values: str) -> str:
    return "\n\n".join(str(value or "").strip() for value in values if str(value or "").strip())


def _record_local_context_trace(trace: Any | None, *contexts: Any) -> None:
    if trace is None:
        return
    refs: list[dict[str, object]] = []
    for context in contexts:
        for node in getattr(context, "selected_nodes", ()) or ():
            refs.append({
                "id": getattr(node, "id", ""),
                "scope": getattr(node, "scope", ""),
                "kind": getattr(node, "kind", ""),
                "source": "local_context",
            })
        for item in getattr(context, "selected_items", ()) or ():
            refs.append({
                "id": getattr(item, "id", ""),
                "scope": getattr(item, "scope", ""),
                "kind": getattr(item, "kind", ""),
                "source": getattr(item, "source", "continuity"),
            })
    if not refs:
        return
    FailOpenPromptTrace(trace).call("record_local_context_refs", refs)


def _record_secondary_input_prepared_trace(
    trace: Any | None,
    phase: str,
    **sections: object,
) -> None:
    if trace is None:
        return
    sink = FailOpenPromptTrace(trace)
    phase_text = str(phase or "secondary").strip() or "secondary"
    for name, text in sections.items():
        if not str(text or ""):
            continue
        sink.record_section(PromptEnvelopeSection(
            name=f"{phase_text}_{name}",
            text=str(text or ""),
            purpose=f"{phase_text} secondary input prepared",
            freshness="secondary_input_prepared",
            source_refs=(f"secondary_input:{phase_text}:{name}",),
        ))


def _record_review_input_prepared_trace(
    trace: Any | None,
    *,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    change_brief: str,
    project_map: str,
    verification_map: str,
    review_impact_map: str,
    execution_evidence: str,
) -> None:
    if trace is None:
        return
    _record_secondary_input_prepared_trace(
        trace,
        "review",
        task=task,
        writer_summary=writer_summary,
        diff=(changes or {}).get("diff", "") if isinstance(changes, dict) else "",
        recent_log=recent_log,
        change_brief=change_brief,
        project_map=project_map,
        verification_map=verification_map,
        review_impact_map=review_impact_map,
        execution_evidence=execution_evidence,
    )


def _record_research_result_trace(trace: Any | None, result: Any) -> None:
    if trace is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_permission_profile", "research", phase="research")
    sink.call(
        "record_research_notes",
        [
            *getattr(result, "notes_created", ()),
            *getattr(result, "notes_updated", ()),
            getattr(result, "synthesis_id", ""),
        ],
    )
    sink.call("record_research_sources", getattr(result, "opened_sources", ()))
    record = getattr(result, "research_record", None)
    if record is None:
        return
    summary = None
    to_summary_payload = getattr(record, "to_summary_payload", None)
    if callable(to_summary_payload):
        summary = to_summary_payload()
    elif isinstance(record, dict):
        summary = record
    if summary is not None:
        sink.call("record_research_record_summary", summary)


def _record_evidence_ledger_write_trace(trace: Any | None, result: Any) -> None:
    if trace is None or result is None:
        return
    to_trace_payload = getattr(result, "to_trace_payload", None)
    if not callable(to_trace_payload):
        return
    FailOpenPromptTrace(trace).call("record_evidence_ledger_write", to_trace_payload())


def _record_research_proof_review_trace(trace: Any | None, review: Any) -> None:
    if trace is None or review is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call(
        "record_research_proof_review",
        proof_review_trace_payload(review),
    )
    sink.call("flush")


def _record_research_plan_trace(
    trace: Any | None,
    review: Any,
    *,
    question: str = "",
) -> None:
    if trace is None or review is None:
        return
    try:
        plan = build_research_plan(review, question=question)
        payload = research_plan_trace_payload(plan)
    except Exception:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_research_plan", payload)
    sink.call("flush")


def _default_research_search_provider() -> ConnectorAwareSearchProvider:
    return ConnectorAwareSearchProvider(BrowserSearchProvider(isolated=False))


def _research_queue_item_title(item: GhostWorkItem | None) -> str:
    if item is None:
        return ""
    if str(getattr(item, "kind", "") or "") not in RESEARCH_QUEUE_KINDS:
        return ""
    return str(getattr(item, "title", "") or "").strip()


# One bounded repair round is the whole of 0.4.13: enough to prove the
# repair-context loop works, small enough that a model stuck in a wrong
# local optimum cannot turn Codey into a self-consuming machine.
MAX_COMPLETION_REPAIR_ROUNDS = 1

# Verified Completion Enforcement stage (A/B treatment definition):
#   "off"    -> 0.4.12 control: shadow proof is trace-only, done unchanged;
#   "block"  -> proof blocks unverifiable done, no repair context admitted;
#   "repair" -> full v1: one bounded repair-context round for product failures.
# Production ships "repair"; the A/B harness overrides the constant per arm.
ENFORCEMENT_OFF = "off"
ENFORCEMENT_BLOCK = "block"
ENFORCEMENT_REPAIR = "repair"
COMPLETION_ENFORCEMENT_MODE = ENFORCEMENT_REPAIR

COMPLETION_REPAIR_FOLLOWUP = (
    "Continue with the established project and JSON tool protocol.\n\n"
    "Your previous completion claim did not pass local verification. The "
    "completion repair context section of this message lists the observed "
    "failure facts. Decide and perform the next local step yourself."
)

_COMPLETION_BLOCKED_NOTE = {
    "unobserved": (
        "Completion blocked: the required verification was never observed "
        "locally. Unobserved is not failure, but it is not done either."
    ),
    "max_repair_rounds": (
        "Completion blocked: local verification still failing after the "
        "repair round."
    ),
    "turn_budget_exhausted": (
        "Completion blocked: local verification still failing and no turn "
        "budget remains for a repair round."
    ),
    "environment_failure": (
        "Completion blocked: the verification command could not run "
        "(environment error), so the failure cannot be attributed to the "
        "code."
    ),
    "provider_failure": (
        "Completion blocked: provider failed during the repair phase."
    ),
    "repair_context_unavailable": (
        "Completion blocked: no safe bounded failure facts were available "
        "for a repair round."
    ),
    "repair_not_admitted": (
        "Completion blocked: local verification failed and this run "
        "admits no repair round."
    ),
}


def _record_completion_proof_trace(
    trace: Any | None,
    proof: object,
) -> None:
    if proof is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_completion_proof", completion_proof_trace_payload(proof))
    sink.call("flush")


def _record_edit_integrity_trace(
    trace: Any | None,
    observation: EditIntegrityObservation | None,
) -> None:
    if observation is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_edit_integrity", observation.to_payload())
    sink.call("flush")


def _blocked_result(result: RunResult, reason: str) -> RunResult:
    """Turn a claimed-done result into an honest blocked stop."""
    note = _COMPLETION_BLOCKED_NOTE.get(
        reason,
        "Completion blocked: local completion proof did not pass.",
    )
    summary = result.summary.strip()
    return replace(
        result,
        stop_reason="blocked",
        summary=f"{summary}\n\n[{note}]" if summary else f"[{note}]",
    )


def _provider_fallback_policy_decision(
    *,
    from_provider: str,
    to_provider: str,
    phase: str,
):
    return evaluate_action(ActionSubject(
        kind="provider_fallback",
        phase=phase,
        from_provider=from_provider,
        to_provider=to_provider,
    ))


NEW_PROJECT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".codey",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    "dist",
    "build",
}
NEW_PROJECT_IGNORED_FILES = {
    ".DS_Store",
    "Thumbs.db",
    ".gitignore",
    ".gitattributes",
    ".gitkeep",
}


def _project_has_user_files(project: str | Path) -> bool:
    """Return true when a project has real user files worth inspecting first."""

    stack = [Path(project).expanduser()]
    while stack:
        current = stack.pop()
        try:
            entries = tuple(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in NEW_PROJECT_IGNORED_DIRS:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    stack.append(entry)
                elif entry.is_file() and name not in NEW_PROJECT_IGNORED_FILES:
                    return True
            except OSError:
                continue
    return False


def _safe_verification_map(
    project: str | Path,
    changes: dict,
    checks: tuple[object, ...],
    project_map: str,
    recommended_commands: tuple[str, ...] = (),
) -> str:
    try:
        return render_verification_map(
            project,
            changes,
            checks_after_last_change=checks,
            project_map=project_map,
            recommended_commands=recommended_commands,
        )
    except Exception:
        return ""


def _resolve_task_kind(request: TaskRequest) -> str:
    intent = (request.intent or "auto").strip().lower()
    if intent in {"planning", "planning_readonly", "readonly"}:
        return "planning_readonly" if request.project else "chat"
    if intent in {"research", "project", "hybrid", "chat", "review"}:
        if intent == "hybrid" and not request.project:
            return "research"
        if intent == "project" and not request.project:
            return "chat"
        if intent == "review" and not request.project:
            return "chat"
        return intent
    return "project" if request.project else "chat"


def _startup_failover_mode(task_kind: str) -> str:
    return "research" if task_kind == "hybrid" else task_kind


def _writer_failover_mode(task_kind: str) -> str:
    return "project" if task_kind == "hybrid" else task_kind


def _ui_mode(kind: str, project: str | None) -> str:
    if kind == "chat":
        return "chat"
    if kind == "research":
        return "research"
    if kind == "hybrid":
        return "hybrid"
    if kind == "planning_readonly":
        return "planning"
    if kind == "review":
        return "review"
    return "agent" if project else "chat"


def _trace_mode(kind: str, project: str | None) -> str:
    if kind == "planning_readonly":
        return "planning"
    if kind in {"chat", "research", "project", "hybrid", "review"}:
        if kind in {"project", "hybrid"} and not project:
            return "chat"
        return kind
    return "project" if project else "chat"


def _bullet_lines(values: tuple[str, ...]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {item}" for item in values)


def _render_review_only_summary(review: object) -> str:
    approved = bool(getattr(review, "approved", False))
    summary = str(getattr(review, "summary", "") or "").strip()
    if approved:
        return f"Review approved: {summary or 'No issues found.'}"
    lines = [f"Review requested changes: {summary or 'Issues found.'}"]
    findings = getattr(review, "findings", ()) or ()
    for index, finding in enumerate(list(findings)[:8], start=1):
        path = str(getattr(finding, "path", "") or "").strip()
        issue = str(getattr(finding, "issue", "") or "").strip()
        fix = str(getattr(finding, "suggested_fix", "") or "").strip()
        prefix = f"{index}. "
        if path:
            prefix += f"{path}: "
        text = issue or "Issue found"
        if fix:
            text += f" Suggested fix: {fix}"
        lines.append(prefix + text)
    return "\n".join(lines)


def _research_payload(result, *, pipeline_result: Any | None = None) -> dict:
    payload = {
        "max_turns_used": int(getattr(result, "max_turns_used", 0) or 0),
        "synthesis_id": result.synthesis_id,
        "notes_created": result.notes_created,
        "notes_updated": result.notes_updated,
        "sources_read": result.sources_read,
        "source_urls": result.source_urls,
        "queries": result.queries,
        "search_results": result.search_results,
        "opened_sources": result.opened_sources,
        "coverage": result.coverage,
        "citation_map": result.citation_map,
        "evidence_items": result.evidence_items,
        "counterpoints": result.counterpoints,
        "quality_warnings": result.quality_warnings,
    }
    if pipeline_result is not None:
        to_payload = getattr(pipeline_result, "to_payload", None)
        metadata = to_payload() if callable(to_payload) else {}
        if isinstance(metadata, dict):
            payload.update({
                "followup_applied": bool(metadata.get("followup_applied")),
                "followup_rounds": max(0, min(3, int(metadata.get("followup_rounds") or 0))),
                "pipeline_stop_reason": str(metadata.get("stop_reason") or ""),
                "planner_stop_reason": str(metadata.get("planner_stop_reason") or ""),
                "fresh_source_count": max(0, int(metadata.get("fresh_source_count") or 0)),
                "new_evidence_count": max(0, int(metadata.get("new_evidence_count") or 0)),
                "final_evidence_count": max(0, int(metadata.get("final_evidence_count") or 0)),
                "attempted_fresh_source_count": max(0, int(metadata.get("attempted_fresh_source_count") or 0)),
                "attempted_new_evidence_count": max(0, int(metadata.get("attempted_new_evidence_count") or 0)),
            })
    return payload





class TaskRunner:
    """Coordinate one task while leaving transport and storage outside."""

    def __init__(
        self,
        state,
        *,
        agent_run: Callable,
        collect_changes: Callable,
        run_review: Callable,
        capture_provider_failure: Callable,
        run_consensus: Callable | None = None,
        run_project_audit: Callable | None = None,
        run_research_advisors: Callable | None = None,
        project_facts: ProjectFactsStore | None = None,
        work_checkpoints: WorkCheckpointStore | None = None,
        run_ledgers: RunLedgerStore | None = None,
        run_traces: RunTraceStore | None = None,
        run_operations: RunOperationStore | None = None,
        evidence_ledgers: EvidenceLedgerStore | None = None,
        capabilities: CapabilityRegistry | None = None,
        managed_outputs: ManagedOutputStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        search_factory: Callable[[], object] | None = None,
        is_git_repository: Callable[[str | Path], bool] | None = None,
        review_fix_turns: int = 12,
        review_log_lines: int = 80,
        ghost_learning_provider_factory: Callable[[str], Any] | None = None,
        ghost_learning_modes: tuple[str, ...] = ("chat",),
        ghost_router_provider_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.state = state
        self.agent_run = agent_run
        self.collect_changes = collect_changes
        self.run_review = run_review
        self.run_consensus = run_consensus
        self.run_project_audit = run_project_audit
        self.run_research_advisors = run_research_advisors
        self.capture_provider_failure = capture_provider_failure
        self.project_facts = project_facts
        self.work_checkpoints = work_checkpoints
        self.run_ledgers = run_ledgers
        self.run_traces = run_traces
        self.run_operations = run_operations
        self.evidence_ledgers = evidence_ledgers
        self.capabilities = capabilities
        self.managed_outputs = managed_outputs
        self.knowledge_store = knowledge_store
        self.search_factory = search_factory or _default_research_search_provider
        self.is_git_repository = is_git_repository or (lambda _project: False)
        self.review_fix_turns = review_fix_turns
        self.review_log_lines = review_log_lines
        self.ghost_learning_provider_factory = ghost_learning_provider_factory
        self.ghost_learning_modes = tuple(str(item or "").strip() for item in ghost_learning_modes)
        self.ghost_router_provider_factory = ghost_router_provider_factory

    def _start_run_operation(
        self,
        work: _RunWork,
        *,
        session_id: str,
        run_id: str,
        project: str,
        provider_id: str,
        turn_budget: int,
        max_repair_rounds: int,
    ) -> None:
        if self.run_operations is None:
            return
        try:
            work.operation = self.run_operations.start(
                session_id=session_id,
                run_id=run_id,
                project=project,
                provider_id=provider_id,
                turn_budget=turn_budget,
                max_repair_rounds=max_repair_rounds,
            )
        except Exception:
            work.operation = None

    def _commit_run_operation(self, work: _RunWork, transition: Callable) -> None:
        # Explanatory persistence is fail-open: one failed commit disables
        # this run's tracking instead of perturbing the coding run.
        if work.operation is None or self.run_operations is None:
            return
        try:
            work.operation = self.run_operations.commit(
                work.operation.session_id,
                work.operation.run_id,
                transition,
            )
        except Exception:
            work.operation = None

    def _finish_run_operation(self, work: _RunWork, event: dict) -> None:
        # Same bounded fields RunLedger.finish persists; the terminal
        # snapshot and the ledger's run_finished row must agree.
        if work.operation is None:
            return
        self._commit_run_operation(work, lambda state: mark_terminal(
            state,
            stop_reason=str(event.get("stop_reason") or ""),
            summary_chars=len(str(event.get("summary") or "")),
            turns=event.get("turns") or 0,
            max_turns=event.get("max_turns") or 0,
            provider=str(event.get("provider") or ""),
        ))

    def _managed_tool_fns(
        self,
        *,
        session_id: str,
        run_id: str,
    ) -> AgentToolFns | None:
        if self.managed_outputs is None:
            return None

        def run_command(
            root: Path,
            rel: str,
            command: str,
            tool_id: str,
            _permission_profile: str,
            _phase: str,
        ):
            return run_command_with_managed_output(
                root,
                rel,
                command,
                permission_profile=_permission_profile,
                phase=_phase,
                store=self.managed_outputs,
                session_id=session_id,
                run_id=run_id,
                tool_id=tool_id,
            )

        return AgentToolFns(run_command_with_context=run_command)

    def _ghost_directive(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ):
        store = getattr(self.state, "ghost_hebbian", None)
        if store is None:
            return build_ghost_directive(None)
        try:
            return build_ghost_directive(
                store,
                project=project,
                session_id=session_id,
                affinity_store=self._ghost_affinity_store(),
            )
        except Exception:
            return build_ghost_directive(None)

    def _ghost_directive_text(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ) -> str:
        return self._ghost_directive(project=project, session_id=session_id).text

    def _ghost_affinity_store(self):
        store = getattr(self.state, "ghost_affinity", None)
        if store is None:
            return None
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None:
            try:
                if not inbox_store.learning_enabled():
                    return None
            except Exception:
                return None
        return store

    def _ghost_continuity(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ):
        store = getattr(self.state, "ghost_continuity", None)
        if store is None:
            return build_ghost_continuity(None)
        try:
            return build_ghost_continuity(
                store,
                project=project,
                session_id=session_id,
            )
        except Exception:
            return build_ghost_continuity(None)

    def _ghost_continuity_text(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ) -> str:
        return self._ghost_continuity(project=project, session_id=session_id).text

    def _maybe_run_ghost_learning(
        self,
        frame: _RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None or self.ghost_learning_provider_factory is None:
            return
        mode = str(event.get("mode") or "")
        if mode not in self.ghost_learning_modes:
            return
        if str(event.get("stop_reason") or "") != "done":
            return
        try:
            loop = GhostLearningLoop(
                signal_store=getattr(self.state, "ghost_signals", None),
                inbox_store=getattr(self.state, "ghost_inbox", None),
                hebbian_store=getattr(self.state, "ghost_hebbian", None),
            )
            result = loop.learn_from_turn(
                GhostLearningTurn(
                    mode=mode,
                    user_text=frame.request.task,
                    assistant_text=str(event.get("summary") or ""),
                    session_id=frame.request.session_id,
                    run_id=frame.run_id,
                    project=frame.project_text if mode != "chat" else "",
                    provider_id=frame.provider_id,
                ),
                provider_factory=self.ghost_learning_provider_factory,
                timeout=DEFAULT_GHOST_LEARNING_TIMEOUT,
                new_chat_timeout=DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
            )
            self.state.emit(result.to_event(
                run_id=frame.run_id,
                session_id=frame.request.session_id,
            ))
        except Exception:
            return

    def _maybe_sync_ghost_continuity(
        self,
        frame: _RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None:
            return
        store = getattr(self.state, "ghost_continuity", None)
        if store is None:
            return
        mode = str(event.get("mode") or "")
        if mode not in {"chat", "planning"}:
            return
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None and not inbox_store.learning_enabled():
            try:
                self.state.emit({
                    "type": "ghost_continuity_done",
                    "run_id": frame.run_id,
                    "session_id": frame.request.session_id,
                    "ok": True,
                    "skipped_reason": "learning_disabled",
                    "items_changed": 0,
                    "total_items": 0,
                    "warnings": [],
                })
            except Exception:
                pass
            return
        try:
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            result = store.sync_from_sources(
                hebbian_store=getattr(self.state, "ghost_hebbian", None),
                run_projection=projection,
                knowledge_store=self.knowledge_store,
                user_focus_excerpt=frame.request.task,
                session_id=frame.request.session_id,
                run_id=frame.run_id,
                project=frame.project_text if mode != "chat" else "",
                mode=mode,
            )
            self.state.emit(result.to_event(
                run_id=frame.run_id,
                session_id=frame.request.session_id,
            ))
        except Exception:
            return

    def _maybe_kick_ghost_sleep(
        self,
        frame: _RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None:
            return
        if str(event.get("stop_reason") or "") != "done":
            return
        kick = getattr(self.state, "kick_ghost_sleep", None)
        if not callable(kick):
            return
        try:
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            kick(
                trigger="post_turn",
                run_id=frame.run_id,
                session_id=frame.request.session_id,
                project=frame.project_text,
                run_projection=projection,
            )
        except Exception:
            return

    def _maybe_claim_ghost_work_item(
        self,
        request: TaskRequest,
        *,
        run_id: str,
    ):
        if str(request.intent or "auto").strip().lower() != "auto":
            return None
        if not is_strict_work_continuation(request.task):
            return None
        store = getattr(self.state, "ghost_work_queue", None)
        if store is None:
            return None
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None:
            try:
                if not inbox_store.learning_enabled():
                    return None
            except Exception:
                return None
        try:
            affinity_hints = ()
            affinity_store = self._ghost_affinity_store()
            if affinity_store is not None:
                try:
                    queued = store.list_items(
                        status="queued",
                        project=request.project or "",
                        session_id=request.session_id,
                    )
                    affinity_hints = affinity_store.query_work_priority_hints(
                        queued,
                        project=request.project or "",
                        session_id=request.session_id,
                    )
                except Exception:
                    affinity_hints = ()
            result = store.claim_next(
                session_id=request.session_id,
                project=request.project or "",
                run_id=run_id,
                user_request=request.task,
                affinity_hints=affinity_hints,
            )
        except Exception:
            return None
        return result if getattr(result, "ok", False) and getattr(result, "item", None) is not None else None

    def _maybe_sync_ghost_work_queue(
        self,
        frame: _RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None:
            return
        store = getattr(self.state, "ghost_work_queue", None)
        affinity_store = self._ghost_affinity_store()
        if store is None and affinity_store is None:
            return
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None:
            try:
                if not inbox_store.learning_enabled():
                    return
            except Exception:
                return
        try:
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            research_interest_candidates = build_research_interest_candidates(
                self.knowledge_store,
                session_id=frame.request.session_id,
                project=frame.project_text,
            )
            if affinity_store is not None and research_interest_candidates:
                hints = affinity_store.query_research_priority_hints(
                    research_interest_candidates,
                    session_id=frame.request.session_id,
                    project=frame.project_text,
                )
                research_interest_candidates = apply_research_affinity_hints(
                    research_interest_candidates,
                    hints,
                )
            if store is not None:
                store.sync_from_sources(
                    continuity_store=getattr(self.state, "ghost_continuity", None),
                    work_checkpoint_store=self.work_checkpoints,
                    run_projection=projection,
                    terminal_event=event,
                    research_interest_candidates=research_interest_candidates,
                    session_id=frame.request.session_id,
                    run_id=frame.run_id,
                    project=frame.project_text,
                )
            if affinity_store is not None:
                affinity_store.sync_from_sources(
                    hebbian_store=getattr(self.state, "ghost_hebbian", None),
                    work_queue_store=store,
                    research_interest_candidates=research_interest_candidates,
                    router_store=getattr(self.state, "ghost_router", None),
                    run_projection=projection,
                    terminal_event=event,
                    session_id=frame.request.session_id,
                    project=frame.project_text,
                )
        except Exception:
            return

    def _maybe_sync_ghost_affinity_terminal_event(
        self,
        request: TaskRequest,
        *,
        run_id: str,
        project_text: str,
        terminal_event: dict[str, object],
    ) -> None:
        affinity_store = self._ghost_affinity_store()
        if affinity_store is None:
            return
        try:
            projection = (
                load_run_projection(self.run_ledgers, request.session_id, run_id)
                if self.run_ledgers is not None
                else None
            )
            affinity_store.sync_from_sources(
                hebbian_store=getattr(self.state, "ghost_hebbian", None),
                work_queue_store=getattr(self.state, "ghost_work_queue", None),
                router_store=getattr(self.state, "ghost_router", None),
                run_projection=projection,
                terminal_event=terminal_event,
                session_id=request.session_id,
                project=project_text,
            )
        except Exception:
            return

    def _maybe_complete_ghost_work_item(
        self,
        frame: _RunFrame | None,
        event: dict[str, object],
        item: GhostWorkItem | None,
        *,
        research_result: Any = None,
    ) -> None:
        if item is None:
            return
        store = getattr(self.state, "ghost_work_queue", None)
        if store is None:
            return
        try:
            run_id = str(event.get("run_id") or getattr(item, "started_run_id", "") or "")
            if frame is None:
                if str(event.get("stop_reason") or "") != "done":
                    store.block_item(
                        item.id,
                        run_id=run_id,
                        blocked_reason=str(event.get("stop_reason") or "run_not_done"),
                    )
                return
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            if str(event.get("stop_reason") or "") == "done":
                if str(getattr(item, "kind", "") or "") in RESEARCH_QUEUE_KINDS:
                    decision = ResearchCompletionGate(self.evidence_ledgers).evaluate(
                        item=item,
                        event=event,
                        research_result=research_result,
                        session_id=frame.request.session_id,
                        project=frame.project_text,
                    )
                    _record_completion_proof_trace(frame.trace, decision.proof)
                    if decision.complete:
                        store.complete_item(
                            item.id,
                            run_id=frame.run_id,
                            proof_refs=decision.proof_refs,
                        )
                    else:
                        _record_research_proof_review_trace(frame.trace, decision.review)
                        _record_research_plan_trace(
                            frame.trace,
                            decision.review,
                            question=_research_queue_item_title(item) or frame.request.task,
                        )
                        store.block_item(
                            item.id,
                            run_id=frame.run_id,
                            blocked_reason=decision.blocked_reason or "research_proof_failed",
                        )
                else:
                    store.complete_item(
                        item.id,
                        run_id=frame.run_id,
                        proof_refs=proof_refs_from_task_event(
                            item,
                            event,
                            run_projection=projection,
                        ),
                    )
            else:
                store.block_item(
                    item.id,
                    run_id=frame.run_id,
                    blocked_reason=str(event.get("stop_reason") or "run_not_done"),
                )
        except Exception:
            return

    def _maybe_release_ghost_work_item(
        self,
        item: GhostWorkItem | None,
        *,
        run_id: str,
        reason: str,
    ) -> None:
        if item is None:
            return
        store = getattr(self.state, "ghost_work_queue", None)
        if store is None:
            return
        try:
            store.release_item(item.id, run_id=run_id, reason=reason)
        except Exception:
            return

    def _maybe_route_auto(
        self,
        request: TaskRequest,
        *,
        baseline_mode: str,
        run_id: str,
    ) -> GhostRouteResult | None:
        intent = str(request.intent or "auto").strip().lower()
        if intent != "auto":
            return None
        store = getattr(self.state, "ghost_router", None)
        if store is None:
            return None
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None and not inbox_store.learning_enabled():
            return None
        provider_factory = self.ghost_router_provider_factory
        if provider_factory is None:
            return None
        route_request = GhostRouteRequest(
            task=request.task,
            baseline_mode=baseline_mode,
            run_id=run_id,
            session_id=request.session_id,
            project=request.project or "",
            provider_id=request.provider_id,
            continue_request=request.continue_task,
            has_reviewable_diff=self._has_reviewable_diff(request.project),
        )
        try:
            with provider_controls.suppress_assistance():
                return GhostRouter(store).route(
                    route_request,
                    provider_factory=provider_factory,
                    timeout=min(DEFAULT_GHOST_ROUTER_TIMEOUT, PRODUCTION_GHOST_ROUTER_TIMEOUT),
                    new_chat_timeout=min(
                        DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
                        PRODUCTION_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
                    ),
                    max_attempts=PRODUCTION_GHOST_ROUTER_ATTEMPTS,
                )
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            raise
        except Exception:
            return None

    def _has_reviewable_diff(self, project: str | None) -> bool:
        if not project:
            return False
        try:
            return has_reviewable_changes(self._collect_review_changes(project))
        except Exception:
            return False

    def _collect_review_changes(self, project: str | None) -> dict:
        if not project:
            return {"ok": False, "error": "project required", "files": [], "diff": ""}
        return self.collect_changes(project, self._review_change_tracker(project))

    def _review_change_tracker(self, project: str | None):
        if not project:
            return None
        try:
            key = str(Path(project).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            return None
        tracker_for = getattr(self.state, "change_tracker_for", None)
        if not callable(tracker_for):
            return None
        try:
            persistent = not self.is_git_repository(key)
        except Exception:
            persistent = True
        try:
            return tracker_for(key, persistent=persistent)
        except Exception:
            return None

    def _event_with_projected_receipt(
        self,
        event: dict,
        *,
        session_id: str,
        run_id: str,
    ) -> dict:
        # The terminal event's receipt must be the receipt the ledger
        # durably recorded, not a parallel in-memory copy. If a run never
        # recorded final changes (research, review, chat, early errors),
        # keep whatever the mode already carried.
        if self.run_ledgers is None:
            return event
        projection = load_run_projection(self.run_ledgers, session_id, run_id)
        receipt = build_task_receipt_from_projection(projection)
        if receipt is None:
            return event
        updated = dict(event)
        updated["receipt"] = receipt.to_dict()
        return updated

    def run(self, request: TaskRequest) -> None:
        state = self.state
        session_id = request.session_id
        project = request.project
        task = request.task
        max_turns = request.max_turns
        continue_task = request.continue_task
        provider_id = request.provider_id
        baseline_task_kind = _resolve_task_kind(request)
        task_kind = baseline_task_kind
        run_id = request.run_id
        claimed_work_item: GhostWorkItem | None = None

        if not run_id:
            reserved = state.reserve_run(
                session_id=session_id,
                project=project,
                task=task,
                provider_id=provider_id,
            )
            if reserved is None:
                return
            run_id = reserved.run_id
        if not state.start_run(run_id):
            return

        trace = None
        if self.run_traces is not None:
            try:
                trace = self.run_traces.open(
                    run_id=run_id,
                    session_id=session_id,
                    project=project,
                    mode_initial=_trace_mode(baseline_task_kind, project),
                    provider_initial=provider_id,
                )
            except Exception:
                trace = None
        trace_sink = FailOpenPromptTrace(trace)

        # One config read per run: failover ranking and the project context
        # builder share this load instead of each re-reading .codey/config.json.
        project_config_result = (
            load_project_config(project) if project else ProjectConfigLoadResult()
        )

        def finish_trace(event: dict[str, object]) -> None:
            status = str(event.get("stop_reason") or "done")
            trace_sink.call(
                "finish",
                status=status,
                mode=_trace_mode(task_kind, project),
                provider=str(event.get("provider") or current_provider_id()),
            )

        provider_controls.set_teach_handler(state.handle_control_teach)
        provider_controls.set_doctor_handler(getattr(state, "handle_profile_doctor", None))
        provider_flow.set_recovery_handler(
            getattr(state, "handle_flow_recovery", None)
        )
        provider_controls.begin_task_context(session_id)
        state.last_provider_failure = None
        previous_cancel_event = cancellation.set_event(state.stop_flag)
        route_result = None
        try:
            claim_result = self._maybe_claim_ghost_work_item(request, run_id=run_id)
            if claim_result is not None:
                claimed_work_item = claim_result.item
                task_kind = claim_result.mode or task_kind
                request = replace(
                    request,
                    task=claim_result.task or request.task,
                    continue_task=True,
                )
                task = request.task
                continue_task = request.continue_task
            else:
                route_result = self._maybe_route_auto(
                    request,
                    baseline_mode=baseline_task_kind,
                    run_id=run_id,
                )
                if route_result is not None:
                    task_kind = route_result.final_mode
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            state.set_provider_session(provider_id, None)
            self._maybe_release_ghost_work_item(
                claimed_work_item,
                run_id=run_id,
                reason="stopped_before_start",
            )
            stopped_event = {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": "",
                "stop_reason": "stopped",
                "turns": 0,
                "max_turns": max_turns,
                "provider": provider_id,
                "mode": _ui_mode(baseline_task_kind, project),
                "provider_failure": None,
            }
            trace_sink.call(
                "record_router",
                baseline_mode=_trace_mode(baseline_task_kind, project),
                selected_mode=_trace_mode(task_kind, project),
                final_mode=_trace_mode(task_kind, project),
                source="local_work_item" if claimed_work_item else "baseline",
                reason_code="stopped_before_start",
            )
            finish_trace(stopped_event)
            state.finish_run(run_id, stopped_event)
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            return
        except BaseException as exc:
            # Any other failure inside the claim/route window must still
            # restore the previous cancellation event and task context, or
            # later runs on this thread inherit our stop flag. The run slot
            # is already started, so it also gets a bounded error terminal
            # event: an exception here must not leave the runner busy
            # forever.
            state.set_provider_session(provider_id, None)
            self._maybe_release_ghost_work_item(
                claimed_work_item,
                run_id=run_id,
                reason="aborted_before_start",
            )
            error_event = {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": f"ERROR: {exc}",
                "stop_reason": "error",
                "turns": 0,
                "max_turns": max_turns,
                "provider": provider_id,
                "mode": _ui_mode(task_kind, project),
                "provider_failure": None,
            }
            finish_trace(error_event)
            state.finish_run(run_id, error_event)
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            raise
        route_source = "explicit_user_choice" if str(request.intent or "").strip().lower() != "auto" else "baseline"
        route_reason = "intent_selected" if route_source == "explicit_user_choice" else "baseline_kept"
        route_selected_mode = task_kind
        if claimed_work_item is not None:
            route_source = "local_work_item"
            route_reason = "claimed_work_item"
        elif route_result is not None:
            route_source = "auto_router"
            route_selected_mode = route_result.selected_mode or route_result.final_mode or task_kind
            route_reason = (
                "accepted"
                if route_result.accepted
                else (route_result.skipped_reason or "baseline_kept")
            )
        trace_sink.call(
            "record_router",
            baseline_mode=_trace_mode(baseline_task_kind, project),
            selected_mode=_trace_mode(route_selected_mode, project),
            final_mode=_trace_mode(task_kind, project),
            source=route_source,
            reason_code=route_reason,
            overridden_by_user=(route_source == "explicit_user_choice"),
        )
        state.emit({
            "type": "task_start",
            "run_id": run_id,
            "session_id": session_id,
            "project": project,
            "task": task,
            "mode": _ui_mode(task_kind, project),
            "max_turns": max_turns,
            "continue_task": continue_task,
            "provider": provider_id,
            "intent": request.intent,
        })

        work = _RunWork(
            recent_events=[],
            evidence=ExecutionEvidence(),
            claimed_work_item=claimed_work_item,
            trace=trace,
        )
        if project and task_kind in {"project", "hybrid", "planning_readonly", "review"} and self.run_ledgers is not None:
            try:
                work.ledger = self.run_ledgers.open(
                    run_id=run_id,
                    session_id=session_id,
                    project=project,
                    task=task,
                    provider=provider_id,
                    mode=_ui_mode(task_kind, project),
                )
                work.record_agent_events_in_ledger = task_kind in {"project", "planning_readonly"}
            except Exception:
                work.ledger = None
        frame: _RunFrame | None = None
        provider: Any | None = None
        logged_provider_failures: set[tuple[str, str, str, str]] = set()

        def append_ledger(action: Callable[[RunLedgerWriter], None]) -> None:
            if work.ledger is None:
                return
            try:
                action(work.ledger)
            except Exception:
                work.ledger = None

        def append_ledger_provider_failure(pid: str, failure: ProviderFailure) -> None:
            key = (
                str(pid),
                str(getattr(failure, "action", "")),
                str(getattr(failure, "kind", "")),
                str(getattr(failure, "message", "")),
            )
            if key in logged_provider_failures:
                return
            logged_provider_failures.add(key)
            append_ledger(
                lambda ledger: ledger.append_provider_failure(pid, failure)
            )

        def current_provider_id() -> str:
            return frame.provider_id if frame is not None else provider_id

        def current_provider() -> Any | None:
            return frame.provider if frame is not None else provider

        def update_checkpoint(
            action: Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint],
        ) -> None:
            if self.work_checkpoints is None or work.work_checkpoint is None:
                return
            try:
                work.work_checkpoint = action(
                    self.work_checkpoints,
                    work.work_checkpoint,
                )
            except (OSError, ValueError):
                pass

        def on_event(event: RunEvent) -> None:
            if work.record_agent_events_in_ledger:
                append_ledger(lambda ledger: ledger.append_run_event(event))
            payload = run_event_ui_payload(run_id, session_id, event)
            if payload is not None:
                state.emit(payload)
            if event.kind == "tool_start":
                return
            work.evidence.record(event)
            message = render_run_event(event)
            work.recent_events.append(message)
            if len(work.recent_events) > self.review_log_lines * 2:
                del work.recent_events[:self.review_log_lines]
            if (
                project
                and event.kind == "tool"
                and event.call is not None
                and event.outcome is not None
            ):
                self._handle_project_tool_event(
                    event=event,
                    project=project,
                    work=work,
                    run_id=run_id,
                    update_checkpoint=update_checkpoint,
                )

        def on_shell_request(cwd_rel: str, command: str) -> None:
            if not project:
                return
            risk = classify_shell_risk(command)
            approval_id = "shell_" + uuid.uuid4().hex[:12]
            pending = {
                "id": approval_id,
                "session_id": session_id,
                "project": project,
                "cwd": cwd_rel or ".",
                "command": command,
                "risk_label": risk.label,
                "risk_title": risk.title,
                "risk_detail": risk.detail,
                "post_approval_instructions": risk.post_approval_instructions,
                "max_turns": max_turns,
                "provider": current_provider_id(),
                "continue_after": True,
                "run_id": run_id,
            }
            pending["ui_event"] = {
                "type": "shell_request",
                "run_id": run_id,
                "session_id": session_id,
                "id": approval_id,
                "project": project,
                "cwd": pending["cwd"],
                "command": command,
                "risk_label": risk.label,
                "risk_title": risk.title,
                "risk_detail": risk.detail,
            }
            with state.lock:
                state.pending_shell[approval_id] = pending
            state.emit(pending["ui_event"])

        try:
            supervisor = getattr(state, "provider_supervisor", None)
            self_repair = getattr(state, "self_repair", None)

            def record_provider_failure(pid: str, failure: ProviderFailure) -> None:
                append_ledger_provider_failure(pid, failure)
                trace_sink.call("record_provider_failure", pid, failure)
                if supervisor is None:
                    return
                health = supervisor.record_failure(pid, failure)
                if self_repair is not None:
                    try:
                        self_repair.maybe_enqueue(pid, failure, health)
                    except Exception:
                        pass

            def provider_failover_order() -> tuple[str, ...]:
                loader = getattr(state, "provider_failover_order", None)
                try:
                    return tuple(loader()) if loader is not None else tuple(PROVIDER_LABELS)
                except Exception:
                    return tuple(PROVIDER_LABELS)

            def ranked_failover_order() -> tuple[str, ...]:
                mode = _startup_failover_mode(task_kind)
                return rank_providers(
                    provider_failover_order(),
                    mode=mode,
                    # Soft preference only: project config re-ranks the
                    # candidates; it cannot override the user's explicit
                    # provider or supervisor availability decisions.
                    preferred=preferred_provider_for(project_config_result.config, mode),
                )

            if task_kind == "review":
                project_text = str(Path(project).expanduser().resolve()) if project else ""
                conversation = state.conversation_for(session_id)
                frame = _RunFrame(
                    request=request,
                    run_id=run_id,
                    task_kind=task_kind,
                    provider=None,
                    provider_id=provider_id,
                    project_text=project_text,
                    conversation=conversation,
                    fresh_chat=False,
                    handoff="",
                    research_handoff="",
                    prior_snapshot=conversation.snapshot,
                    recovered_owner_prompt="",
                    provider_session_changed=False,
                    preflight_tried=set(),
                    preflight_switches=0,
                    trace=trace,
                )
                outcome = self._run_review_mode(frame)
                append_ledger(lambda ledger: ledger.finish(**outcome.event))
                event = self._event_with_projected_receipt(
                    outcome.event,
                    session_id=session_id,
                    run_id=run_id,
                )
                self._finish_run_operation(work, event)
                finish_trace(event)
                state.finish_run(run_id, event)
                self._maybe_complete_ghost_work_item(
                    frame,
                    event,
                    work.claimed_work_item,
                    research_result=outcome.research_result,
                )
                self._maybe_run_ghost_learning(frame, event)
                self._maybe_sync_ghost_continuity(frame, event)
                self._maybe_sync_ghost_work_queue(frame, event)
                self._maybe_kick_ghost_sleep(frame, event)
                return

            preflight_tried: set[str] = set()
            preflight_switches = 0
            if supervisor is not None:
                supervisor.prepare_user_selected(provider_id)
            if supervisor is not None and not supervisor.is_available(provider_id):
                replacement_id = supervisor.select(
                    "",
                    ranked_failover_order(),
                    excluded=(provider_id,),
                )
                if replacement_id is not None:
                    previous_provider_id = provider_id
                    provider_id = replacement_id
                    state.switch_run_provider(run_id, provider_id)
                    append_ledger(
                        lambda ledger: ledger.append(
                            "provider_switched",
                            from_provider=previous_provider_id,
                            to_provider=provider_id,
                            phase="preflight",
                            reason="unavailable",
                        )
                    )
                    trace_sink.call(
                        "record_fallback",
                        from_provider=previous_provider_id,
                        to_provider=provider_id,
                        phase="preflight",
                        reason_code="unavailable",
                    )
                    trace_sink.call(
                        "record_policy_decision",
                        _provider_fallback_policy_decision(
                            from_provider=previous_provider_id,
                            to_provider=provider_id,
                            phase="preflight",
                        ),
                    )
                    preflight_switches = 1
                else:
                    raise RuntimeError("selected provider is unavailable")
            while True:
                preflight_tried.add(provider_id)
                try:
                    provider = state.get_provider(provider_id)
                except cancellation.TaskCancelled:
                    raise
                except Exception as connect_error:
                    failure = self.capture_provider_failure(
                        model=PROVIDER_LABELS.get(provider_id, provider_id),
                        action="connect",
                        page=None,
                        error=connect_error,
                    )
                    record_provider_failure(provider_id, failure)
                    if preflight_switches >= 2:
                        raise ProviderActionError(failure) from connect_error
                    replacement_id = (
                        supervisor.select(
                            "",
                            ranked_failover_order(),
                            excluded=preflight_tried,
                        )
                        if supervisor is not None
                        else next(
                            (
                                item
                                for item in ranked_failover_order()
                                if item not in preflight_tried
                            ),
                            None,
                        )
                    )
                    if replacement_id is None:
                        raise ProviderActionError(failure) from connect_error
                    previous_provider_id = provider_id
                    provider_id = replacement_id
                    preflight_switches += 1
                    state.switch_run_provider(run_id, provider_id)
                    append_ledger(
                        lambda ledger: ledger.append(
                            "provider_switched",
                            from_provider=previous_provider_id,
                            to_provider=provider_id,
                            phase="connect",
                            reason="provider_failure",
                        )
                    )
                    trace_sink.call(
                        "record_fallback",
                        from_provider=previous_provider_id,
                        to_provider=provider_id,
                        phase="connect",
                        reason_code="provider_failure",
                    )
                    trace_sink.call(
                        "record_policy_decision",
                        _provider_fallback_policy_decision(
                            from_provider=previous_provider_id,
                            to_provider=provider_id,
                            phase="connect",
                        ),
                    )
                    continue
                if (
                    supervisor is None
                    or not supervisor.needs_canary(provider_id)
                    or run_half_open_canary(provider_id, provider, supervisor)
                ):
                    break
                try:
                    provider.close()
                except Exception:
                    pass
                if preflight_switches >= 2:
                    raise RuntimeError("no healthy provider available after canary failure")
                replacement_id = supervisor.select(
                    "",
                    ranked_failover_order(),
                    excluded=preflight_tried,
                )
                if replacement_id is None:
                    raise RuntimeError("no healthy provider available after canary failure")
                previous_provider_id = provider_id
                provider_id = replacement_id
                preflight_switches += 1
                state.switch_run_provider(run_id, provider_id)
                append_ledger(
                    lambda ledger: ledger.append(
                        "provider_switched",
                        from_provider=previous_provider_id,
                        to_provider=provider_id,
                        phase="canary",
                        reason="provider_failure",
                    )
                )
                trace_sink.call(
                    "record_fallback",
                    from_provider=previous_provider_id,
                    to_provider=provider_id,
                    phase="canary",
                    reason_code="provider_failure",
                )
                trace_sink.call(
                    "record_policy_decision",
                    _provider_fallback_policy_decision(
                        from_provider=previous_provider_id,
                        to_provider=provider_id,
                        phase="canary",
                    ),
                )
            if task_kind == "research":
                mode = "research"
            elif task_kind == "planning_readonly":
                mode = "planning"
            elif task_kind in {"project", "hybrid"}:
                mode = "project" if project else "chat"
            else:
                mode = "chat"
            project_text = str(Path(project).expanduser().resolve()) if project else ""
            conversation = state.conversation_for(session_id)
            provider_session_changed = state.provider_session_changed(
                provider_id,
                session_id,
            )
            can_summarize_current_chat = (
                conversation.initialized
                and provider_id == conversation.provider_id
                and not provider_session_changed
            )
            fresh_chat, handoff = conversation.plan_request(
                provider_id=provider_id,
                mode=mode,
                project=project_text,
                force_rollover=continue_task or provider_session_changed,
                next_prompt=task,
            )
            if fresh_chat and can_summarize_current_chat:
                def send_handoff_summary(summary_prompt: str) -> str:
                    record_provider_send_prompt(
                        trace,
                        name="conversation_handoff_summary_prompt",
                        text=summary_prompt,
                        purpose="conversation handoff summary prompt sent to provider",
                        source_ref="provider_send:conversation_handoff_summary",
                        capability_id="conversation_handoff",
                    )
                    return provider.send(summary_prompt)

                handoff = conversation.prepare_model_handoff(send_handoff_summary)
            prior_snapshot = conversation.snapshot
            recovered_owner_prompt = ""
            visible_excerpt = ""
            if fresh_chat or task_kind in {"research", "hybrid"}:
                try:
                    visible_excerpt = state.visible_session_excerpt(
                        session_id,
                        current_request=task,
                    )
                except Exception:
                    visible_excerpt = ""
            research_handoff = ""
            if task_kind in {"research", "hybrid"}:
                if handoff or visible_excerpt:
                    research_handoff = render_recovered_handoff(
                        prior_snapshot,
                        visible_excerpt,
                    )
                elif prior_snapshot.to_payload():
                    research_handoff = render_handoff(prior_snapshot)
            conversation.update_snapshot(ConversationSnapshot(
                mode=mode,
                goal=prior_snapshot.goal or task,
                project=project_text,
                provider_id=prior_snapshot.provider_id,
                changed_files=prior_snapshot.changed_files,
                checks_passed=prior_snapshot.checks_passed,
                summary=prior_snapshot.summary,
                blocker=prior_snapshot.blocker,
                latest_user=task if mode == "chat" else "",
                latest_reply=prior_snapshot.latest_reply if mode == "chat" else "",
                conversation_summary=prior_snapshot.conversation_summary,
            ))
            if fresh_chat:
                if handoff or visible_excerpt:
                    handoff = render_recovered_handoff(
                        prior_snapshot,
                        visible_excerpt,
                    )
                if visible_excerpt:
                    recovered_owner_prompt = handoff
            frame = _RunFrame(
                request=request,
                run_id=run_id,
                task_kind=task_kind,
                provider=provider,
                provider_id=provider_id,
                project_text=project_text,
                conversation=conversation,
                fresh_chat=fresh_chat,
                handoff=handoff,
                research_handoff=research_handoff,
                prior_snapshot=prior_snapshot,
                recovered_owner_prompt=recovered_owner_prompt,
                provider_session_changed=provider_session_changed,
                preflight_tried=preflight_tried,
                preflight_switches=preflight_switches,
                trace=trace,
            )
            hooks = _RunHooks(
                on_event=on_event,
                on_shell_request=on_shell_request,
                update_checkpoint=update_checkpoint,
                record_provider_failure=record_provider_failure,
                append_ledger=append_ledger,
                provider_failover_order=provider_failover_order,
                supervisor=supervisor,
                trace=trace,
            )
            if task_kind == "research":
                outcome = self._run_research_mode(
                    frame,
                    hooks,
                    proof_question=_research_queue_item_title(work.claimed_work_item),
                )
            elif task_kind == "hybrid":
                outcome = self._run_hybrid_mode(
                    frame,
                    work,
                    hooks,
                    config_result=project_config_result,
                )
            elif task_kind == "planning_readonly":
                outcome = self._run_planning_readonly_mode(
                    frame,
                    work,
                    config_result=project_config_result,
                )
            elif task_kind == "project":
                outcome = self._run_project_mode(
                    frame,
                    work,
                    hooks,
                    config_result=project_config_result,
                )
            else:
                outcome = self._run_chat_mode(frame)
            append_ledger(lambda ledger: ledger.finish(**outcome.event))
            event = self._event_with_projected_receipt(
                outcome.event,
                session_id=session_id,
                run_id=run_id,
            )
            self._finish_run_operation(work, event)
            finish_trace(event)
            state.finish_run(run_id, event)
            self._maybe_complete_ghost_work_item(
                frame,
                event,
                work.claimed_work_item,
                research_result=outcome.research_result,
            )
            self._maybe_run_ghost_learning(frame, event)
            self._maybe_sync_ghost_continuity(frame, event)
            self._maybe_sync_ghost_work_queue(frame, event)
            self._maybe_kick_ghost_sleep(frame, event)
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            current_id = current_provider_id()
            state.set_provider_session(current_id, None)
            update_checkpoint(
                lambda store, item: store.set_status(item, "interrupted", "stopped")
            )
            current_conversation = (
                frame.conversation
                if frame is not None
                else (conversation if "conversation" in locals() else None)
            )
            if current_conversation is not None:
                current_conversation.update_snapshot(replace(
                    current_conversation.snapshot,
                    provider_id=current_id,
                    blocker="stopped",
                ))
            stopped_event = {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": "",
                "stop_reason": "stopped",
                "turns": 0,
                "max_turns": max_turns,
                "provider": current_id,
                "mode": _ui_mode(task_kind, project),
                "provider_failure": None,
            }
            append_ledger(lambda ledger: ledger.finish(**stopped_event))
            stopped_event = self._event_with_projected_receipt(
                stopped_event,
                session_id=session_id,
                run_id=run_id,
            )
            self._finish_run_operation(work, stopped_event)
            finish_trace(stopped_event)
            state.finish_run(run_id, stopped_event)
            self._maybe_release_ghost_work_item(
                work.claimed_work_item if "work" in locals() else claimed_work_item,
                run_id=run_id,
                reason="stopped",
            )
        except Exception as exc:
            current_id = current_provider_id()
            current_item = current_provider()
            update_checkpoint(
                lambda store, item: store.set_status(item, "interrupted", "error")
            )
            current_conversation = (
                frame.conversation
                if frame is not None
                else (conversation if "conversation" in locals() else None)
            )
            if current_conversation is not None:
                current_conversation.update_snapshot(replace(
                    current_conversation.snapshot,
                    provider_id=current_id,
                    blocker=str(exc),
                ))
            failure = (
                exc.failure
                if isinstance(exc, ProviderActionError)
                else self.capture_provider_failure(
                    model=PROVIDER_LABELS.get(current_id, current_id),
                    action="task" if current_item is not None else "connect",
                    page=None,
                    error=exc,
                )
            )
            state.last_provider_failure = failure
            if failure is not None:
                append_ledger_provider_failure(current_id, failure)
            error_event = {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": f"ERROR: {exc}",
                "stop_reason": "error",
                "turns": 0,
                "max_turns": max_turns,
                "provider": current_id,
                "mode": _ui_mode(task_kind, project),
                "provider_failure": failure.to_dict() if failure else None,
            }
            append_ledger(lambda ledger: ledger.finish(**error_event))
            error_event = self._event_with_projected_receipt(
                error_event,
                session_id=session_id,
                run_id=run_id,
            )
            self._finish_run_operation(work, error_event)
            finish_trace(error_event)
            state.finish_run(run_id, error_event)
            self._maybe_complete_ghost_work_item(
                frame,
                error_event,
                work.claimed_work_item if "work" in locals() else claimed_work_item,
            )
            if frame is not None:
                self._maybe_sync_ghost_work_queue(frame, error_event)
            else:
                self._maybe_sync_ghost_affinity_terminal_event(
                    request,
                    run_id=run_id,
                    project_text=str(project or ""),
                    terminal_event=error_event,
                )
        finally:
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            try:
                current_item = current_provider()
                if current_item is not None:
                    current_item.close()
            except Exception:
                pass

    def _run_research_mode(
        self,
        frame: _RunFrame,
        hooks: _RunHooks,
        *,
        proof_question: str = "",
    ) -> _ModeOutcome:
        state = self.state
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        pipeline_result = self._run_research_pipeline(
            frame,
            hooks,
            max_turns=request.max_turns,
            proof_question=proof_question,
        )
        result = pipeline_result.final_result
        state.set_provider_session(
            frame.provider_id,
            None if result.stop_reason == "stopped" else request.session_id,
        )
        frame.conversation.begin_window(
            frame.provider_id,
            "research",
            frame.project_text,
        )
        frame.conversation.record_exchange(
            request.task,
            result.summary,
            replace(
                frame.conversation.snapshot,
                mode="research",
                goal=request.task,
                project=frame.project_text,
                provider_id=frame.provider_id,
                blocker="" if result.stop_reason == "done" else result.summary,
                latest_user=request.task,
                latest_reply=result.summary,
                summary=result.summary,
            ),
        )
        receipt = {
            "display": {"summary": result.receipt},
            "work": {
                "created": result.notes_created,
                "updated": result.notes_updated,
                "synthesis_id": result.synthesis_id,
            },
        }
        return _ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": int(getattr(result, "max_turns_used", 0) or request.max_turns),
            "provider": frame.provider_id,
            "mode": "research",
            "receipt": receipt,
            "research": _research_payload(result, pipeline_result=pipeline_result),
        }, research_result=result, research_pipeline_result=pipeline_result)

    def _run_hybrid_mode(
        self,
        frame: _RunFrame,
        work: _RunWork,
        hooks: _RunHooks,
        *,
        config_result: ProjectConfigLoadResult | None = None,
    ) -> _ModeOutcome:
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        pipeline_result = self._run_research_pipeline(
            frame,
            hooks,
            max_turns=max(1, min(request.max_turns, 18)),
        )
        research_result = pipeline_result.final_result
        if research_result.stop_reason != "done":
            return _ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": research_result.summary,
                "stop_reason": research_result.stop_reason,
                "turns": research_result.turns,
                "max_turns": int(getattr(research_result, "max_turns_used", 0) or request.max_turns),
                "provider": frame.provider_id,
                "mode": "research",
                "receipt": {"display": {"summary": research_result.receipt}},
                "research": _research_payload(research_result, pipeline_result=pipeline_result),
            }, research_result=research_result, research_pipeline_result=pipeline_result)
        frame.fresh_chat = True
        frame.handoff = ""
        frame.conversation.update_snapshot(replace(
            frame.conversation.snapshot,
            mode="research",
            goal=request.task,
            project=frame.project_text,
            provider_id=frame.provider_id,
            summary=research_result.summary,
            blocker="",
            latest_user=request.task,
            latest_reply=research_result.summary,
        ))
        return self._run_project_mode(
            frame,
            work,
            hooks,
            config_result=config_result,
            research_result=research_result,
            research_pipeline_result=pipeline_result,
        )

    def _run_chat_mode(self, frame: _RunFrame) -> _ModeOutcome:
        state = self.state
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        if frame.fresh_chat:
            frame.provider.new_chat()
        prompt = (
            render_continuation_prompt(frame.handoff, request.task)
            if frame.handoff
            else request.task
        )
        ghost_directive = self._ghost_directive(
            session_id=request.session_id,
        )
        ghost_continuity = self._ghost_continuity(session_id=request.session_id)
        _record_local_context_trace(frame.trace, ghost_directive, ghost_continuity)
        ghost_context = _join_local_contexts(
            ghost_directive.text,
            ghost_continuity.text,
        )
        prompt = _prepend_ghost_directive(prompt, ghost_context)
        trace = FailOpenPromptTrace(frame.trace)
        trace.call("record_permission_profile", "chat", phase="chat")
        consulted = None
        if self.run_consensus is not None:
            compact_context = (
                render_handoff(frame.prior_snapshot)
                if frame.fresh_chat and frame.handoff
                else (
                    render_handoff(frame.conversation.snapshot)
                    if frame.conversation.initialized
                    else ""
                )
            )
            try:
                owner_prompt = _owner_prompt_with_ghost_directive(
                    frame.recovered_owner_prompt,
                    ghost_context,
                )
                _record_secondary_input_prepared_trace(
                    frame.trace,
                    "consensus",
                    task=request.task,
                    context=compact_context,
                    owner_prompt=owner_prompt,
                )
                consulted = self.run_consensus(
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=compact_context,
                    draft_first=True,
                    owner_prompt=owner_prompt,
                    trace_recorder=frame.trace,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                state.set_provider_session(frame.provider_id, None)
                raise
        if consulted is not None:
            reply = consulted.answer
        else:
            record_provider_send_prompt(
                frame.trace,
                name="chat_outbound_prompt",
                text=prompt,
                purpose="chat prompt sent to provider",
                source_ref="provider_send:chat",
                capability_id="chat_runner",
            )
            reply = frame.provider.send(prompt)
        if frame.fresh_chat:
            frame.conversation.begin_window(frame.provider_id, "chat")
        state.set_provider_session(
            frame.provider_id,
            None if consulted is not None and consulted.degraded else request.session_id,
        )
        frame.conversation.record_exchange(
            prompt,
            reply,
            replace(
                frame.conversation.snapshot,
                provider_id=frame.provider_id,
                blocker="",
                latest_user=request.task,
                latest_reply=reply,
            ),
        )
        state.emit({
            "type": "reply",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "text": reply,
        })
        result = RunResult(reply, "done", 1)
        return _ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "chat",
        })

    def _run_planning_readonly_mode(
        self,
        frame: _RunFrame,
        work: _RunWork,
        *,
        config_result: ProjectConfigLoadResult | None = None,
    ) -> _ModeOutcome:
        state = self.state
        request = frame.request
        project = request.project
        if project is None:
            raise RuntimeError("planning_readonly mode requires a project")
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        if work.ledger is not None:
            work.record_agent_events_in_ledger = True
        context_builder = ProjectTaskContextBuilder(
            project_facts=self.project_facts,
            work_checkpoints=None,
            knowledge_store=self.knowledge_store,
            config_result=config_result,
        )
        project_context = context_builder.build(
            project=project,
            task=request.task,
            session_id=request.session_id,
            run_id=frame.run_id,
            continue_task=False,
            provider_session_changed=frame.provider_session_changed,
        )
        ghost_directive = self._ghost_directive(
            project=project,
            session_id=request.session_id,
        )
        ghost_continuity = self._ghost_continuity(
            project=project,
            session_id=request.session_id,
        )
        _record_local_context_trace(frame.trace, ghost_directive, ghost_continuity)
        result = self.agent_run(
            frame.provider,
            Path(project),
            request.task,
            max_turns=request.max_turns,
            on_event=lambda event: self._planning_event(frame, work, event),
            on_shell_request=None,
            stop_flag=state.stop_flag,
            fresh_chat=frame.fresh_chat,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=frame.conversation,
            provider_id=frame.provider_id,
            handoff=frame.handoff,
            project_facts=project_context.verified_facts,
            research_context=project_context.research_context,
            project_map=project_context.project_map,
            project_config_warnings=project_context.project_config_warnings,
            ghost_directive=ghost_directive.text,
            ghost_continuity=ghost_continuity.text,
            permission_profile="planning_readonly",
            trace_recorder=frame.trace,
        )
        state.set_provider_session(
            frame.provider_id,
            None if result.stop_reason == "stopped" else request.session_id,
        )
        frame.conversation.update_snapshot(replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            checks_passed=False,
            summary=result.summary,
            blocker="" if result.stop_reason == "done" else result.summary,
        ))
        return _ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "planning",
            "changed": False,
        })

    def _run_review_mode(self, frame: _RunFrame) -> _ModeOutcome:
        state = self.state
        request = frame.request
        project = request.project
        trace = FailOpenPromptTrace(frame.trace)
        trace.call("record_permission_profile", "reviewer", phase="review")
        trace.record_section(PromptEnvelopeSection(
            name="review_request",
            text=request.task,
            purpose="review request from the user",
            freshness="run_start",
            source_refs=("request:review",),
        ))
        if project is None:
            summary = "No attached project is available to review."
            state.emit({
                "type": "review",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "text": summary,
            })
            return _ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": summary,
                "stop_reason": "done",
                "turns": 0,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "review",
                "changed": False,
            })
        changes = self._collect_review_changes(project)
        trace.record_section(PromptEnvelopeSection(
            name="review_changes",
            text=changes.get("diff", "") if isinstance(changes, dict) else "",
            purpose="bounded local diff prepared for review",
            freshness="run_start",
            source_refs=("local_diff:review",),
        ))
        if not isinstance(changes, dict) or changes.get("ok") is not True:
            summary = "Could not collect a local diff to review."
            state.emit({
                "type": "review",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "text": summary,
            })
            return _ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": summary,
                "stop_reason": "done",
                "turns": 0,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "review",
                "changed": False,
            })
        if not has_reviewable_changes(changes):
            summary = "No reviewable local diff was found."
            state.emit({
                "type": "review",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "text": summary,
            })
            return _ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": summary,
                "stop_reason": "done",
                "turns": 0,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "review",
                "changed": False,
                "changes": {
                    "changed_count": changes.get("changed_count", 0),
                    "files": changes.get("files", [])[:3],
                    "mode": changes.get("mode"),
                    "project": project,
                },
            })
        try:
            try:
                review_impact_map = safe_review_impact_map(project, changes)
            except cancellation.TaskCancelled:
                raise
            except Exception:
                review_impact_map = ""
            _record_review_input_prepared_trace(
                frame.trace,
                task=request.task,
                writer_summary="Review-only mode did not run a writer.",
                changes=changes,
                recent_log="",
                change_brief="",
                project_map="",
                verification_map="",
                review_impact_map=review_impact_map,
                execution_evidence="",
            )
            reviewed = self.run_review(
                session_id=request.session_id,
                project=project,
                task=request.task,
                writer_summary="Review-only mode did not run a writer.",
                changes=changes,
                recent_log="",
                writer_id=frame.provider_id,
                change_brief="",
                project_map="",
                verification_map="",
                review_impact_map=review_impact_map,
                execution_evidence="",
                trace_recorder=frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            reviewed = None
        if reviewed is None:
            summary = "Review unavailable. No files were changed."
        else:
            _reviewer_id, review = reviewed
            summary = _render_review_only_summary(review)
        state.set_provider_session(frame.provider_id, None)
        frame.conversation.update_snapshot(replace(
            frame.conversation.snapshot,
            mode="review",
            goal=request.task,
            project=frame.project_text,
            provider_id=frame.provider_id,
            changed_files=changed_paths_from_changes(changes),
            checks_passed=False,
            summary=summary,
            blocker="",
            latest_user=request.task,
            latest_reply=summary,
        ))
        state.emit({
            "type": "review",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "text": summary,
        })
        return _ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": summary,
            "stop_reason": "done",
            "turns": 1 if reviewed is not None else 0,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "review",
            "changed": False,
            "changes": {
                "changed_count": changes.get("changed_count", 0),
                "files": changes.get("files", [])[:3],
                "mode": changes.get("mode"),
                "project": project,
            },
        })

    def _planning_event(
        self,
        frame: _RunFrame,
        work: _RunWork,
        event: RunEvent,
    ) -> None:
        if work.record_agent_events_in_ledger and work.ledger is not None:
            try:
                work.ledger.append_run_event(event)
            except Exception:
                work.ledger = None
        payload = run_event_ui_payload(frame.run_id, frame.request.session_id, event)
        if payload is not None:
            self.state.emit(payload)
        if event.kind == "tool_start":
            return
        work.evidence.record(event)
        message = render_run_event(event)
        work.recent_events.append(message)
        if len(work.recent_events) > self.review_log_lines * 2:
            del work.recent_events[:self.review_log_lines]

    def _run_project_mode(
        self,
        frame: _RunFrame,
        work: _RunWork,
        hooks: _RunHooks,
        *,
        config_result: ProjectConfigLoadResult | None = None,
        research_result=None,
        research_pipeline_result=None,
    ) -> _ModeOutcome:
        state = self.state
        request = frame.request
        project = request.project
        if project is None:
            raise RuntimeError("project mode requires a project")
        if work.ledger is not None:
            work.record_agent_events_in_ledger = True
        context_builder = ProjectTaskContextBuilder(
            project_facts=self.project_facts,
            work_checkpoints=self.work_checkpoints,
            knowledge_store=self.knowledge_store,
            config_result=config_result,
        )
        project_context = context_builder.build(
            project=project,
            task=request.task,
            session_id=request.session_id,
            run_id=frame.run_id,
            continue_task=request.continue_task,
            provider_session_changed=frame.provider_session_changed,
        )
        verified_facts = project_context.verified_facts
        verification_verified_commands = (
            project_context.verification_verified_commands
        )
        verification_candidates = project_context.verification_candidates
        resumed_verification_commands = (
            project_context.resumed_verification_commands
        )
        configured_verification_commands = (
            project_context.configured_verification_commands
        )
        configured_ignored_paths = project_context.configured_ignored_paths
        project_map_chars = project_context.project_map_chars
        project_map = project_context.project_map
        work.work_checkpoint = project_context.checkpoint.item
        checkpoint_prompt = project_context.checkpoint.prompt
        resumed_changed_files = project_context.checkpoint.changed_files
        resumed_successful_checks = project_context.checkpoint.successful_checks
        work.evidence.seed_checks(project_context.checkpoint.seed_checks)
        agent_task = request.task
        change_brief: ChangeBrief | None = None
        agent_fresh_chat = frame.fresh_chat
        has_user_files = _project_has_user_files(project)
        used_project_audit = False
        if self.run_consensus is not None and not has_user_files:
            context = render_project_context(
                frame.conversation.snapshot,
                verified_facts,
                project_map=project_map,
            )
            try:
                _record_secondary_input_prepared_trace(
                    frame.trace,
                    "consensus",
                    task=request.task,
                    context=context,
                )
                planned = self.run_consensus(
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=context,
                    plan=True,
                    draft_first=True,
                    trace_recorder=frame.trace,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                state.set_provider_session(frame.provider_id, None)
                agent_fresh_chat = True
                planned = None
            if planned is not None:
                change_brief = new_project_change_brief(request.task, planned.answer)
                agent_task = change_brief.apply_to_task(request.task)
                agent_fresh_chat = True
        elif self.run_project_audit is not None and has_user_files:
            context = render_project_context(
                frame.conversation.snapshot,
                verified_facts,
                project_map=project_map,
            )
            try:
                _record_secondary_input_prepared_trace(
                    frame.trace,
                    "project_audit",
                    task=request.task,
                    context=context,
                )
                reports = self.run_project_audit(
                    project=project,
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=context,
                    trace_recorder=frame.trace,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                reports = ()
            if reports:
                change_brief = project_audit_change_brief(request.task, reports)
                agent_task = change_brief.apply_to_task(request.task)
                used_project_audit = True
        key = str(Path(project).expanduser().resolve())
        tracker = state.change_tracker_for(
            key,
            persistent=not self.is_git_repository(key),
        )
        tried_writers = set(frame.preflight_tried)
        # One-shot holder for the repair phase: run_one_writer_attempt passes
        # the admitted projection into agent.run(), whose ContextSource
        # machinery renders it and binds the admission row to the outbound
        # send epoch. It is empty for every normal writer attempt.
        repair_projection: RepairContextProjection | None = None
        self._start_run_operation(
            work,
            session_id=request.session_id,
            run_id=frame.run_id,
            project=project,
            provider_id=frame.provider_id,
            turn_budget=request.max_turns,
            max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
        )

        def refresh_checkpoint_view() -> CheckpointView:
            nonlocal checkpoint_prompt
            nonlocal resumed_changed_files
            nonlocal resumed_successful_checks
            refreshed = context_builder.refresh_checkpoint(work.work_checkpoint)
            if refreshed.item is None:
                checkpoint_prompt = ""
                resumed_changed_files = ()
                resumed_successful_checks = ()
            else:
                work.work_checkpoint = refreshed.item
                checkpoint_prompt = refreshed.prompt
                resumed_changed_files = refreshed.changed_files
                resumed_successful_checks = refreshed.successful_checks
            if refreshed.workspace_changed:
                work.evidence.invalidate_checks()
            return CheckpointView(
                prompt=checkpoint_prompt,
                changed_files=resumed_changed_files,
                successful_checks=resumed_successful_checks,
            )

        def run_one_writer_attempt(
            spec: WriterAttempt,
            note_turn: Callable[[int], None],
        ) -> RunResult:
            def on_writer_event(event: RunEvent) -> None:
                note_turn(event.turn)
                hooks.on_event(event)

            return self.agent_run(
                spec.provider,
                Path(project),
                spec.task,
                max_turns=spec.remaining_turns,
                on_event=on_writer_event,
                on_shell_request=hooks.on_shell_request,
                stop_flag=state.stop_flag,
                fresh_chat=spec.fresh_chat,
                strict_fresh_chat=spec.strict_fresh_chat,
                change_tracker=tracker,
                conversation=frame.conversation,
                provider_id=spec.provider_id,
                handoff=spec.handoff,
                project_facts=verified_facts,
                research_context=project_context.research_context,
                project_map=project_map,
                project_config_warnings=project_context.project_config_warnings,
                work_checkpoint=spec.checkpoint.prompt,
                verification_candidates=verification_candidates,
                verification_candidate_loader=lambda: (
                    safe_verification_candidates(
                        project,
                        verification_verified_commands,
                        resumed_verification_commands,
                        configured_verification_commands,
                        configured_ignored_paths,
                    )
                ),
                verification_changed_files=spec.checkpoint.changed_files,
                verification_successful_checks=(
                    spec.checkpoint.successful_checks
                ),
                ghost_directive="",
                ghost_continuity="",
                completion_repair_context=(
                    repair_projection.prompt_text
                    if repair_projection is not None
                    else ""
                ),
                completion_repair_context_payload=(
                    repair_projection.to_payload()
                    if repair_projection is not None
                    else None
                ),
                permission_profile="coding_writer",
                tool_fns=self._managed_tool_fns(
                    session_id=request.session_id,
                    run_id=frame.run_id,
                ),
                trace_recorder=frame.trace,
            )

        def select_next_writer(excluded: set[str]) -> str | None:
            mode = _writer_failover_mode(frame.task_kind)
            preference = (
                preferred_provider_for(config_result.config, mode)
                if config_result is not None
                else ""
            )
            ranked_order = rank_providers(
                hooks.provider_failover_order(),
                mode=mode,
                preferred=preference,
            )
            if hooks.supervisor is not None:
                return hooks.supervisor.select(
                    "",
                    ranked_order,
                    excluded=excluded,
                )
            return next(
                (
                    item
                    for item in ranked_order
                    if item not in excluded
                ),
                None,
            )

        def capture_writer_failure(
            pid: str,
            action: str,
            error: BaseException,
        ) -> ProviderFailure:
            return self.capture_provider_failure(
                model=PROVIDER_LABELS.get(pid, pid),
                action=action,
                page=None,
                error=error,
            )

        def on_writer_switch(next_provider_id: str) -> None:
            previous_provider_id = frame.provider_id
            state.switch_run_provider(frame.run_id, next_provider_id)
            hooks.append_ledger(
                lambda ledger: ledger.append(
                    "provider_switched",
                    from_provider=previous_provider_id,
                    to_provider=next_provider_id,
                    phase="writer_failover",
                    reason="provider_failure",
                )
            )
            FailOpenPromptTrace(hooks.trace).call(
                "record_fallback",
                from_provider=previous_provider_id,
                to_provider=next_provider_id,
                phase="writer_failover",
                reason_code="provider_failure",
            )
            FailOpenPromptTrace(hooks.trace).call(
                "record_policy_decision",
                _provider_fallback_policy_decision(
                    from_provider=previous_provider_id,
                    to_provider=next_provider_id,
                    phase="writer_failover",
                ),
            )
            frame.conversation.update_snapshot(replace(
                frame.conversation.snapshot,
                provider_id=next_provider_id,
                blocker="",
            ))

        failover = WriterFailoverRunner(
            provider=frame.provider,
            provider_id=frame.provider_id,
            switches=frame.preflight_switches,
            tried=tried_writers,
            attempt=run_one_writer_attempt,
            select_next=select_next_writer,
            connect=state.get_provider,
            close=lambda item: item.close(),
            needs_canary=(
                hooks.supervisor.needs_canary
                if hooks.supervisor is not None
                else (lambda _pid: False)
            ),
            run_canary=(
                lambda pid, item: run_half_open_canary(pid, item, hooks.supervisor)
            ),
            capture_failure=capture_writer_failure,
            record_failure=hooks.record_provider_failure,
            record_success=(
                hooks.supervisor.record_success
                if hooks.supervisor is not None
                else (lambda _pid: None)
            ),
            clear_session=lambda pid: state.set_provider_session(pid, None),
            on_switch=on_writer_switch,
            refresh_checkpoint=refresh_checkpoint_view,
            stopped=state.stop_flag.is_set,
        )

        self._commit_run_operation(
            work,
            lambda state: mark_writer_running(state, provider_id=frame.provider_id),
        )

        try:
            result = failover.run(
                task=agent_task,
                turn_budget=request.max_turns,
                fresh=agent_fresh_chat,
                handoff=frame.handoff,
                checkpoint=CheckpointView(
                    prompt=checkpoint_prompt,
                    changed_files=resumed_changed_files,
                    successful_checks=resumed_successful_checks,
                ),
            )
        finally:
            frame.provider = failover.provider
            frame.provider_id = failover.provider_id
            frame.preflight_switches = failover.switches
        self._commit_run_operation(
            work,
            lambda state: mark_writer_settled(
                state,
                provider_id=frame.provider_id,
                turns_used=result.turns,
                stop_reason=result.stop_reason,
            ),
        )
        # Narrow checkpoint-resume green inheritance: the workspace did not
        # change and nothing new ran, so prior green checks still cover it.
        # The receipt stays green, but the completion proof now records this
        # explicitly as stance=inherited_pass / source=checkpoint -- never as
        # this round's clean verification fact (0.4.13 provenance debt).
        inherited_green = bool(
            project_context.checkpoint.resumed
            and work.work_checkpoint is not None
            and not result.changed
            and not result.checks_ran
            and work.evidence.has_successful_checks
        )
        if inherited_green:
            result = replace(result, checks_passed=True)
        checkpoint_changed = bool(
            work.work_checkpoint is not None and work.work_checkpoint.changed_files
        )
        task_changed = result.changed or checkpoint_changed
        task_changes = self.collect_changes(project, tracker)
        collected_changed = change_state(task_changes)
        task_changes_dirty = collected_changed is None
        if collected_changed is not None:
            task_changed = collected_changed
        if result.stop_reason == "done":
            hooks.update_checkpoint(
                lambda store, item: store.set_status(item, "ready_for_review")
            )
        state.set_provider_session(
            frame.provider_id,
            None if result.stop_reason == "stopped" else request.session_id,
        )
        if (
            self.run_consensus is not None
            and not used_project_audit
            and result.stop_reason == "done"
            and not result.changed
            and not state.stop_flag.is_set()
        ):
            context = render_project_context(
                frame.conversation.snapshot,
                verified_facts,
                draft=result.summary,
                project_map=project_map,
            )
            try:
                _record_secondary_input_prepared_trace(
                    frame.trace,
                    "consensus",
                    task=request.task,
                    context=context,
                    draft=result.summary,
                )
                consulted = self.run_consensus(
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=context,
                    draft=result.summary,
                    trace_recorder=frame.trace,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                state.set_provider_session(frame.provider_id, None)
                consulted = None
            if consulted is not None:
                if consulted.degraded:
                    state.set_provider_session(frame.provider_id, None)
                result = replace(result, summary=consulted.answer)
        review_coordinator = ReviewCoordinator(self.collect_changes)

        def render_review_change_brief() -> str:
            return (
                change_brief.render(audience="reviewer")
                if change_brief is not None
                else ""
            )

        def refresh_review_project_map() -> str:
            nonlocal verified_facts
            nonlocal project_map
            nonlocal verification_candidates
            verified_facts = (
                self.project_facts.render(project)
                if self.project_facts is not None
                else ""
            )
            verification_candidates = safe_verification_candidates(
                project,
                verification_verified_commands,
                resumed_verification_commands,
                configured_verification_commands,
                configured_ignored_paths,
            )
            project_map = safe_project_map(
                project,
                verified_facts,
                request.task,
                verification_candidate_lines(verification_candidates),
                ignored_paths=configured_ignored_paths,
                max_chars=project_map_chars,
            )
            return project_map

        def close_writer_for_review() -> None:
            if frame.provider is not None:
                try:
                    frame.provider.close()
                except Exception:
                    pass
            frame.provider = None
            failover.provider = None

        def repair_writer(
            followup: str,
            checkpoint: CheckpointView,
        ) -> RunResult:
            try:
                return failover.run(
                    task=followup,
                    turn_budget=min(request.max_turns, self.review_fix_turns),
                    fresh=False,
                    handoff="",
                    checkpoint=checkpoint,
                )
            finally:
                frame.provider = failover.provider
                frame.provider_id = failover.provider_id
                frame.preflight_switches = failover.switches

        def set_checkpoint_status(status: str) -> None:
            hooks.update_checkpoint(
                lambda store, item: store.set_status(
                    item,
                    status,
                )
            )

        def emit_review_unavailable() -> None:
            state.emit({
                "type": "review",
                "session_id": request.session_id,
                "text": "Unavailable. Continued with one model.",
            })

        def run_review_with_trace(**kwargs):
            try:
                review_impact_map = safe_review_impact_map(
                    kwargs.get("project") or project,
                    kwargs.get("changes") if isinstance(kwargs.get("changes"), dict) else {},
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                review_impact_map = ""
            _record_review_input_prepared_trace(
                frame.trace,
                task=str(kwargs.get("task") or ""),
                writer_summary=str(kwargs.get("writer_summary") or ""),
                changes=kwargs.get("changes") if isinstance(kwargs.get("changes"), dict) else {},
                recent_log=str(kwargs.get("recent_log") or ""),
                change_brief=str(kwargs.get("change_brief") or ""),
                project_map=str(kwargs.get("project_map") or ""),
                verification_map=str(kwargs.get("verification_map") or ""),
                review_impact_map=review_impact_map,
                execution_evidence=str(kwargs.get("execution_evidence") or ""),
            )
            kwargs["review_impact_map"] = review_impact_map
            kwargs["trace_recorder"] = frame.trace
            return self.run_review(**kwargs)

        review_cycle = review_coordinator.run_cycle(
            project=project,
            tracker=tracker,
            session_id=request.session_id,
            task=request.task,
            result=result,
            task_changed=task_changed,
            changes=task_changes,
            changes_dirty=task_changes_dirty,
            writer_id=frame.provider_id,
            recent_log="\n".join(work.recent_events[-self.review_log_lines:]),
            render_change_brief=render_review_change_brief,
            execution_evidence=work.evidence.render_for_review(),
            successful_checks=work.evidence.successful_checks,
            checkpoint_prompt=checkpoint_prompt,
            checks_before_review_followup=(
                work.evidence.has_successful_checks
                or (
                    not work.evidence.observed_tool_events
                    and result.checks_passed
                )
            ),
            stop_requested=state.stop_flag.is_set,
            refresh_project_map=refresh_review_project_map,
            build_verification_map=lambda changes, current_project_map: (
                _safe_verification_map(
                    project,
                    changes,
                    work.evidence.successful_checks,
                    current_project_map,
                    selected_verification_candidate_lines(
                        verification_candidates,
                        changed_paths_from_changes(changes),
                    ),
                )
            ),
            run_review=run_review_with_trace,
            close_writer_for_review=close_writer_for_review,
            repair_writer=repair_writer,
            set_checkpoint_status=set_checkpoint_status,
            emit_review_unavailable=emit_review_unavailable,
        )
        result = review_cycle.result
        task_changed = review_cycle.task_changed
        task_changes = review_cycle.changes
        task_changes_dirty = review_cycle.changes_dirty
        if task_changes is None or task_changes_dirty:
            task_changes = self.collect_changes(project, tracker)
        collected_changed = change_state(task_changes)
        if collected_changed is not None:
            task_changed = collected_changed

        def enforcement_scope(
            changes: dict | None,
            changed: bool,
        ) -> tuple[bool, tuple[str, ...]]:
            files = tuple(
                str(item.get("path") or "")
                for item in ((changes or {}).get("files") or [])
                if item.get("path")
            )
            if (
                COMPLETION_ENFORCEMENT_MODE != ENFORCEMENT_OFF
                and not files
                and change_state(changes) is None
                and work.evidence.changed_files
            ):
                # Changes collection produced no usable verdict while real
                # edits were observed locally: scope enforcement from the
                # observed edits instead of letting an edited run slip past
                # enforcement as "unchanged". A measured net-empty diff --
                # the model reverted its own edit -- is a verdict, so it
                # keeps the run out of scope with an honest unchanged
                # receipt.
                return True, tuple(work.evidence.changed_files)
            return changed, files

        task_changed, files = enforcement_scope(task_changes, task_changed)
        verification_candidates = safe_verification_candidates(
            project,
            verification_verified_commands,
            resumed_verification_commands,
            configured_verification_commands,
            configured_ignored_paths,
        )
        # --- Verified Completion Enforcement (0.4.13) --------------------
        # The first decision point where local facts constrain done: build
        # the completion proof, admit at most one bounded repair context for
        # an observed product failure, then let the FINAL proof drive
        # receipt, ledger, project facts, and the user-visible event.
        selected_check = (
            select_verification_candidate(verification_candidates, files)
            if result.stop_reason == "done" and task_changed and files
            else None
        )
        checkpoint_green = inherited_green or review_cycle.inherited_checks_passed
        verification_forbidden = task_forbids_verification(request.task)

        # The decision inputs are passed explicitly at every call site: the
        # repair round re-collects changes and re-selects the candidate, and
        # the integrity observation must read the exact same snapshot of
        # changes/files/check as the decision it qualifies -- never a diff
        # captured before the repair.
        def completion_decision(
            *,
            stop: str,
            changed: bool,
            scope_files: tuple[str, ...],
            check: object,
            diagnostic_refs: tuple[str, ...] = (),
        ) -> CompletionDecision:
            return build_completion_decision(
                run_id=frame.run_id,
                stop_reason=stop,
                task_changed=changed,
                files=scope_files,
                selected_check=check,
                evidence=work.evidence,
                analysis_run_payloads=work.analysis_run_payloads,
                project=project,
                checkpoint_green=checkpoint_green,
                verification_forbidden=verification_forbidden,
                diagnostic_refs=diagnostic_refs,
            )

        def completion_evidence(
            *,
            changes: object,
            changed: bool,
            scope_files: tuple[str, ...],
            check: object,
            stop: str,
        ) -> tuple[CompletionDecision, EditIntegrityObservation]:
            # Integrity is observed against the decision's verification
            # stance over the SAME snapshot; when it finds anything, the
            # proof is recomputed once so its diagnostic_refs name that
            # observation.
            decided = completion_decision(
                stop=stop,
                changed=changed,
                scope_files=scope_files,
                check=check,
            )
            integrity = observe_edit_integrity(
                task=request.task,
                changes=changes,
                diff=(
                    str(changes.get("diff") or "")
                    if isinstance(changes, dict)
                    else ""
                ),
                files=scope_files,
                decision=decided,
                selected_check=check,
                run_id=frame.run_id,
            )
            if integrity.diagnostic_refs:
                decided = completion_decision(
                    stop=stop,
                    changed=changed,
                    scope_files=scope_files,
                    check=check,
                    diagnostic_refs=integrity.diagnostic_refs,
                )
            return decided, integrity

        def commit_operation_proof(proof: object) -> None:
            # Refs/status only; the proof body stays in the run trace. The
            # facts pass through uncoerced: the strict helper validates them
            # and refuses anything it cannot record honestly.
            if proof is None:
                return
            self._commit_run_operation(
                work,
                lambda state: mark_completion_proof_recorded(
                    state,
                    proof_ref=getattr(proof, "proof_id", ""),
                    proof_status=getattr(proof, "status", ""),
                    proof_satisfied=getattr(proof, "satisfied", None),
                ),
            )

        decision, integrity = completion_evidence(
            changes=task_changes,
            changed=task_changed,
            scope_files=files,
            check=selected_check,
            stop=result.stop_reason,
        )
        proof = decision.proof
        _record_completion_proof_trace(frame.trace, proof)
        _record_edit_integrity_trace(frame.trace, integrity)
        commit_operation_proof(proof)

        blocked_reason = ""
        repaired_once = False
        remaining_turns = request.max_turns - result.turns
        if (
            COMPLETION_ENFORCEMENT_MODE != ENFORCEMENT_OFF
            and proof is not None
            and not proof.satisfied
            and not state.stop_flag.is_set()
            and remaining_turns > 0
            and repair_candidate(
                proof.status,
                decision.failure_class,
                max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
            )
        ):
            if COMPLETION_ENFORCEMENT_MODE == ENFORCEMENT_BLOCK:
                # Treatment arm without repair admission: the failed proof
                # blocks done below, but no failure facts go back to the
                # model and no extra writer turn runs.
                repaired_once = False
            else:
                projection = project_repair_context(
                    proof=proof.to_payload(),
                    failure_class=decision.failure_class,
                    decisive_checks=(
                        decisive_failure_fact(
                            selected_check,
                            work.evidence,
                            files,
                            root=project,
                        ),
                    ),
                    changed_files=files,
                    analysis_run_refs=decision.analysis_run_refs,
                )
                if not projection.admitted:
                    blocked_reason = "repair_context_unavailable"
                else:
                    repair_projection = projection
                    self._commit_run_operation(
                        work,
                        lambda state: mark_repair_context_admitted(
                            state,
                            context_ref=str(projection.to_payload().get("digest") or ""),
                        ),
                    )
                    hooks.on_event(RunEvent.status(
                        "[runner] completion proof did not pass; running one bounded repair round."
                    ))
                    self._commit_run_operation(
                        work,
                        lambda state: mark_repair_running(
                            state,
                            provider_id=frame.provider_id,
                        ),
                    )
                    try:
                        repair_result = failover.run(
                            task=COMPLETION_REPAIR_FOLLOWUP,
                            turn_budget=remaining_turns,
                            fresh=False,
                            handoff="",
                            checkpoint=refresh_checkpoint_view(),
                        )
                    except cancellation.TaskCancelled:
                        raise
                    except ProviderActionError:
                        blocked_reason = "provider_failure"
                        self._commit_run_operation(
                            work,
                            lambda state: mark_repair_settled(
                                state,
                                provider_id=frame.provider_id,
                                stop_reason="",
                                blocked_reason="provider_failure",
                            ),
                        )
                    else:
                        self._commit_run_operation(
                            work,
                            lambda state: mark_repair_settled(
                                state,
                                provider_id=frame.provider_id,
                                stop_reason=repair_result.stop_reason,
                                turns_used=result.turns + repair_result.turns,
                            ),
                        )
                    finally:
                        repair_projection = None
                    repaired_once = not blocked_reason
                    if repaired_once:
                        # The repair is bounded by the shared remaining turn
                        # budget, so the sum can never exceed max_turns.
                        turns = result.turns + repair_result.turns
                        if repair_result.stop_reason == "stopped":
                            result = replace(repair_result, turns=turns)
                        elif repair_result.stop_reason == "done":
                            # Re-collect post-repair facts; the new proof decides.
                            task_changes = self.collect_changes(project, tracker)
                            collected = change_state(task_changes)
                            if collected is not None:
                                task_changed = collected
                            task_changed, files = enforcement_scope(
                                task_changes,
                                task_changed,
                            )
                            verification_candidates = safe_verification_candidates(
                                project,
                                verification_verified_commands,
                                resumed_verification_commands,
                                configured_verification_commands,
                                configured_ignored_paths,
                            )
                            selected_check = (
                                select_verification_candidate(verification_candidates, files)
                                if files
                                else None
                            )
                            result = RunResult(
                                summary=repair_result.summary,
                                stop_reason="done",
                                turns=turns,
                                checks_passed=False,
                                changed=result.changed or repair_result.changed,
                                checks_ran=result.checks_ran or repair_result.checks_ran,
                            )
                            decision, integrity = completion_evidence(
                                changes=task_changes,
                                changed=task_changed,
                                scope_files=files,
                                check=selected_check,
                                stop=result.stop_reason,
                            )
                            proof = decision.proof
                            _record_completion_proof_trace(frame.trace, proof)
                            _record_edit_integrity_trace(frame.trace, integrity)
                            commit_operation_proof(proof)
                        else:
                            result = replace(repair_result, turns=turns)
                            if repair_result.stop_reason != "approval":
                                blocked_reason = "max_repair_rounds"

        if COMPLETION_ENFORCEMENT_MODE == ENFORCEMENT_OFF:
            # 0.4.12 control semantics: the shadow proof stays trace-only and
            # the narrow local-green override decides the receipt, exactly as
            # before enforcement existed.
            legacy_green = bool(
                selected_check is not None
                and work.evidence.observed_tool_events
                and any(
                    check_covers_selected_candidate(
                        selected_check,
                        item.command,
                        item.cwd,
                        files,
                        root=project,
                    )
                    for item in work.evidence.successful_checks
                )
            )
            if selected_check is not None and work.evidence.observed_tool_events:
                result = replace(result, checks_passed=legacy_green)
            # Control keeps the pre-enforcement receipt flag untouched
            # (override when a decisive local green existed, else the
            # model-reported value): nothing else may contaminate the arm.
        elif (
            not blocked_reason
            and result.stop_reason == "done"
            and proof is not None
            and proof.status in ("failed", "blocked")
        ):
            # A done claim backed by a failed or unverifiable proof must
            # never pass as done. complete_with_limitations (docs-only,
            # inherited green) stays an allowed -- but honest -- done. The
            # repair_rounds fact comes from this run's operation state
            # position: a round actually ran only when the repair arm
            # settled without a provider failure.
            blocked_reason = completion_blocked_reason(
                proof_status=proof.status,
                failure_class=decision.failure_class,
                remaining_turns=request.max_turns - result.turns,
                repair_rounds=1 if repaired_once else 0,
            )
        if (
            blocked_reason
            and work.operation is not None
            and work.operation.phase == PHASE_COMPLETION_PROOF_RECORDED
        ):
            # The verdict lands on the durable counter at the decision
            # point; provider-failure and stop verdicts are already
            # carried by their own settled phases.
            self._commit_run_operation(
                work,
                lambda state: mark_completion_blocked(state, reason=blocked_reason),
            )

        if COMPLETION_ENFORCEMENT_MODE != ENFORCEMENT_OFF:
            verified = False
            if blocked_reason and result.stop_reason in (
                "done",
                "max_turns",
                "no_progress",
                "protocol",
            ):
                # Explicit stop conditions win; everything else becomes an
                # honest blocked result instead of a claimed done.
                result = _blocked_result(result, blocked_reason)
            elif result.stop_reason == "done":
                if proof is not None:
                    verified = decision.provenance.stance in (
                        STANCE_FRESH_PASS,
                        STANCE_INHERITED_PASS,
                    )
                else:
                    # Out of enforcement scope (no changed files): keep the
                    # pre-enforcement flag semantics.
                    verified = bool(result.checks_passed)
            result = replace(result, checks_passed=verified)

        receipt = build_task_receipt(
            task_changes,
            decision=decision,
            integrity=integrity,
            checks_passed=result.checks_passed,
        )
        hooks.append_ledger(
            lambda ledger: ledger.append_changes_collected(
                task_changes,
                checks_passed=result.checks_passed,
                receipt=receipt.to_dict(),
            )
        )
        facts_write_required = (
            self.project_facts is not None
            and result.stop_reason == "done"
            and task_changed
            and result.checks_passed
            and receipt.verification.trust == VERIFICATION_TRUST_TRUSTED
            and work.evidence.has_successful_checks
            and files
        )
        facts_write_succeeded = not facts_write_required
        if facts_write_required:
            try:
                fact_task = (
                    work.work_checkpoint.original_task
                    if project_context.checkpoint.resumed
                    and work.work_checkpoint is not None
                    else request.task
                )
                facts_write_succeeded = (
                    self.project_facts.record_successful_change(
                        project,
                        task=fact_task,
                        files=files,
                        checks=work.evidence.successful_checks,
                        receipt=receipt.display.summary,
                    )
                )
            except (OSError, ValueError):
                facts_write_succeeded = False
        if facts_write_succeeded and facts_write_required:
            self._record_project_memory(
                project=project,
                session_id=request.session_id,
                task=request.task,
                files=files,
                receipt=receipt.display.summary,
                checks=work.evidence.successful_checks,
            )
        if self.work_checkpoints is not None and work.work_checkpoint is not None:
            if result.stop_reason == "done" and facts_write_succeeded:
                try:
                    self.work_checkpoints.delete(request.session_id)
                    work.work_checkpoint = None
                except OSError:
                    pass
            elif result.stop_reason != "done":
                hooks.update_checkpoint(
                    lambda store, item: store.set_status(
                        item,
                        "interrupted",
                        result.stop_reason,
                    )
                )
        # Terminal state for this run: now -- and only now -- drop snapshot
        # baselines whose files are back to their original content. UI
        # polling during a run never prunes.
        try:
            tracker.prune_clean()
        except Exception:
            pass
        frame.conversation.update_snapshot(replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            changed_files=files,
            checks_passed=result.checks_passed,
            summary=result.summary,
            blocker="" if result.stop_reason == "done" else result.summary,
        ))
        event = {
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "hybrid" if research_result is not None else "agent",
            "changed": task_changed,
            "receipt": receipt.to_dict(),
        }
        if task_changed and task_changes and task_changes.get("ok"):
            event["changes"] = {
                "changed_count": task_changes.get("changed_count", 0),
                "files": task_changes.get("files", [])[:3],
                "mode": task_changes.get("mode"),
                "project": project,
            }
        if research_result is not None:
            event["research"] = _research_payload(
                research_result,
                pipeline_result=research_pipeline_result,
            )
        return _ModeOutcome(
            event,
            research_result=research_result,
            research_pipeline_result=research_pipeline_result,
        )

    def _run_research_iteration(
        self,
        *,
        provider,
        session_id: str,
        project: str,
        task: str,
        max_turns: int,
        on_event: Callable[[RunEvent], None],
        stop_flag,
        provider_id: str,
        run_id: str,
        chat_handoff: str,
        trace_recorder,
        search,
        tools=None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload: dict[str, object] | None = None,
    ) -> ResearchIterationRun:
        if self.knowledge_store is None:
            raise RuntimeError("Research is not configured")
        runner = ResearchRunner(
            provider,
            search,
            self.knowledge_store,
            max_turns=max_turns,
            should_stop=stop_flag.is_set if stop_flag is not None else None,
            session_id=session_id,
            project=project,
            chat_handoff=chat_handoff,
            permission_profile="research",
            trace_recorder=trace_recorder,
            run_id=run_id,
            review_advisors=(
                (lambda pack: self.run_research_advisors(
                    selected_provider=provider,
                    selected_provider_id=provider_id,
                    pack=pack,
                ))
                if self.run_research_advisors is not None
                else None
            ),
            tools=tools,
            iteration_context=iteration_context,
            topic_continuity_context=topic_continuity_context,
            topic_continuity_payload=topic_continuity_payload,
        )
        for event in runner.run(task):
            on_event(event)
        if runner.result is None:
            raise RuntimeError("research finished without a result")
        return ResearchIterationRun(result=runner.result, tools=runner.tools)

    def _build_research_topic_continuity(
        self,
        *,
        session_id: str,
        project: str,
        trace: Any | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        """Admit bounded Ghost-to-Research topic continuity.

        Single wiring point for the 0.4.12 admission chain: profile gate ->
        research interests + bounded Ghost continuity + prior evidence-ledger
        claim refs -> pure projection. Fail-open by contract: any error or a
        closed gate returns the empty baseline so Research behavior is
        unchanged; failures leave one bounded ``warn`` reason code in the run
        trace instead of disappearing silently. The returned payload is
        digest-only and never contains raw hint text.
        """
        try:
            profile = profile_for_name("research")
            if not allows_context_source(profile, TOPIC_CONTINUITY_CONTEXT_SOURCE_KEY):
                return "", None
            interest_hints = [
                candidate_to_topic_hint(candidate)
                for candidate in build_research_interest_candidates(
                    self.knowledge_store,
                    session_id=session_id,
                    project=project,
                )
            ]
            continuity = self._ghost_continuity(project=project, session_id=session_id)
            projection = project_topic_continuity(
                interest_hints=interest_hints,
                continuity_hints=tuple(getattr(continuity, "selected_items", ()) or ()),
                claim_refs=self._prior_claim_refs(session_id=session_id, project=project),
            )
        except cancellation.TaskCancelled:
            raise
        except cancellation.DeadlineExceeded:
            raise
        except Exception:
            FailOpenPromptTrace(trace).call(
                "warn",
                "research_topic_continuity_projection_failed",
            )
            return "", None
        payload = projection.to_payload()
        if not projection.admitted:
            return "", payload
        return projection.prompt_text, payload

    def _prior_claim_refs(
        self,
        *,
        session_id: str,
        project: str,
    ) -> tuple[dict[str, object], ...]:
        """Bounded claim refs from the durable evidence ledger (refs only).

        Collects up to ``MAX_TOPIC_CLAIM_REFS + 1`` refs: the extra entry is
        the overflow signal the projection needs to report ``truncated``
        honestly, without ever carrying claim text.
        """
        ledgers = self.evidence_ledgers
        if ledgers is None:
            return ()
        try:
            snapshot = ledgers.load(session_id=session_id, project=project)
        except Exception:
            return ()
        payload = getattr(snapshot, "payload", None)
        if not getattr(snapshot, "available", False) or not isinstance(payload, Mapping):
            return ()
        refs: list[dict[str, object]] = []
        for record in list(payload.get("records") or ())[-4:]:
            for claim_ref in record.get("claim_refs") or ():
                text = str(claim_ref or "").strip()
                if not text:
                    continue
                refs.append({"ref": f"prior_claim:{text}"})
                if len(refs) > MAX_TOPIC_CLAIM_REFS:
                    break
            if len(refs) > MAX_TOPIC_CLAIM_REFS:
                break
        return tuple(refs)

    def _build_research_context(
        self,
        frame: _RunFrame,
        request: TaskRequest,
        *,
        proof_question: str,
        max_turns: int,
    ) -> ResearchContext:
        """Assemble the ResearchContext, including continuity admission."""
        continuity_text, continuity_payload = self._build_research_topic_continuity(
            session_id=request.session_id,
            project=frame.project_text,
            trace=frame.trace,
        )
        return ResearchContext(
            question=request.task,
            session_id=request.session_id,
            run_id=frame.run_id,
            project=frame.project_text,
            provider_id=frame.provider_id,
            proof_question=proof_question,
            permission_profile="research",
            max_turns=max_turns,
            chat_handoff=frame.research_handoff,
            should_stop=self.state.stop_flag.is_set,
            trace=RunTraceResearchSink(frame.trace),
            topic_continuity_context=continuity_text,
            topic_continuity_payload=continuity_payload,
        )

    def _run_research_pipeline(
        self,
        frame: _RunFrame,
        hooks: _RunHooks,
        *,
        max_turns: int,
        proof_question: str = "",
    ):
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")

        def run_iteration(
            *,
            task: str,
            max_turns: int,
            chat_handoff: str,
            search: object,
            tools=None,
            iteration_context: str = "",
            topic_continuity_context: str = "",
            topic_continuity_payload=None,
        ):
            return self._run_research_iteration(
                provider=frame.provider,
                session_id=request.session_id,
                project=frame.project_text,
                task=task,
                max_turns=max_turns,
                on_event=hooks.on_event,
                stop_flag=self.state.stop_flag,
                provider_id=frame.provider_id,
                run_id=frame.run_id,
                chat_handoff=chat_handoff,
                trace_recorder=frame.trace,
                search=search,
                tools=tools,
                iteration_context=iteration_context,
                topic_continuity_context=topic_continuity_context,
                topic_continuity_payload=topic_continuity_payload,
            )

        def run_followup(
            *,
            tools,
            plan,
            material,
            question: str,
            initial_summary: str = "",
            max_context_chars: int = 8000,
            should_stop=None,
        ):
            return run_evidence_followup(
                provider=frame.provider,
                tools=tools,
                plan=plan,
                material=material,
                question=question,
                initial_summary=initial_summary,
                max_context_chars=max_context_chars,
                should_stop=should_stop,
            )

        recorder = getattr(self.state, "record_research_changes", None)
        changes_sink = recorder if callable(recorder) else None
        context = self._build_research_context(
            frame,
            request,
            proof_question=proof_question,
            max_turns=max_turns,
        )
        pipeline = ResearchPipeline(
            context=context,
            run_iteration=run_iteration,
            search_factory=self.search_factory,
            evidence_followup_runner=run_followup,
            evidence_ledgers=self.evidence_ledgers,
            config=ResearchPipelineConfig(),
            ledger_event_sink=lambda result: self._record_evidence_ledger_write(hooks, result),
            research_changes_sink=changes_sink,
        )
        return pipeline.run()

    def _record_evidence_ledger_write(
        self,
        hooks: _RunHooks,
        result: EvidenceLedgerWriteResult,
    ) -> None:
        payload = result.to_trace_payload()
        hooks.append_ledger(
            lambda ledger: ledger.append(
                "evidence_ledger_write",
                ok=payload.get("ok"),
                skipped=payload.get("skipped"),
                reason_code=payload.get("reason_code"),
                ledger_ref=payload.get("ledger_ref"),
                record_id=payload.get("record_id"),
                counts=payload.get("counts"),
            )
        )

    def _handle_project_tool_event(
        self,
        *,
        event: RunEvent,
        project: str,
        work: _RunWork,
        run_id: str,
        update_checkpoint: Callable[
            [Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint]],
            None,
        ],
    ) -> None:
        call = event.call
        outcome = event.outcome
        if call is None or outcome is None:
            return
        name = str(call.name or "")
        if name == "run":
            command = str(call.args.get("command") or "")
            cwd = str(call.args.get("path") or ".")
            ok = bool(outcome.ok and outcome.exit_code == 0)
            try:
                tool_index = int(event.metadata.get("tool_index") or 0)
            except (TypeError, ValueError):
                tool_index = 0
            tool_id = f"{event.turn}:{max(0, tool_index)}"
            if ok and self.project_facts is not None:
                try:
                    self.project_facts.record_success(project, cwd, command)
                except (OSError, ValueError):
                    pass
            update_checkpoint(
                lambda store, item: store.record_run(
                    item,
                    command=command,
                    cwd=cwd,
                    ok=ok,
                )
            )
            self._record_analysis_run(
                work=work,
                project=project,
                run_id=run_id,
                tool_id=tool_id,
                tool_name=name,
                command=command,
                cwd=cwd,
                ok=ok,
                outcome=outcome,
            )
        elif name == "edit" and outcome.ok and outcome.changed:
            rel = str(call.args.get("path") or "")
            update_checkpoint(lambda store, item: store.record_edit(item, rel))

    def _record_analysis_run(
        self,
        *,
        work: _RunWork,
        project: str,
        run_id: str,
        tool_id: str,
        tool_name: str,
        command: str,
        cwd: str,
        ok: bool,
        outcome: Any,
    ) -> None:
        """Project one audited run-command execution into the run trace.

        Fail-open by contract: projection or trace failures never affect the
        running task, its receipt, or the model-visible tool result.
        """

        trace = work.trace
        if trace is None or not command:
            return
        try:
            audit = outcome.audit if isinstance(outcome.audit, Mapping) else {}
            # Only real executions become AnalysisRun records. Policy denials,
            # invalid cwd, and command-not-found outcomes carry no timing and
            # must stay out of the execution audit (roadmap: record existing
            # executions, not attempts).
            if not audit.get("command_started_at"):
                return
            managed = outcome.managed_output()
            record = analysis_run_record({
                "run_id": run_id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "command": command,
                "cwd": cwd,
                "project": project,
                "exit_code": outcome.exit_code,
                "ok": ok,
                "started_at": audit.get("command_started_at"),
                "finished_at": audit.get("command_finished_at"),
                "duration_ms": audit.get("command_duration_ms"),
                "managed_output": dict(managed) if managed else {},
            })
            if record is None:
                return
            record_payload = record.to_payload()
            trace.record_analysis_run(record_payload)
            work.analysis_run_payloads.append(record_payload)
            if len(work.analysis_run_payloads) > MAX_ANALYSIS_RUNS:
                del work.analysis_run_payloads[:-MAX_ANALYSIS_RUNS]

            artifact_payload: dict[str, object] | None = None
            if managed:
                artifact = artifact_ref_from_managed_output({
                    **managed,
                    "origin_run_id": run_id,
                    "produced_by": record.analysis_run_id,
                })
                if artifact is not None:
                    artifact_payload = artifact.to_payload()
                    trace.record_artifact_refs([artifact_payload])
                    work.artifact_payloads.append(artifact_payload)
                    if len(work.artifact_payloads) > MAX_ARTIFACT_REFS:
                        del work.artifact_payloads[:-MAX_ARTIFACT_REFS]

            capsule = build_reproducibility_capsule(
                run_id=run_id,
                analysis_runs=work.analysis_run_payloads,
                artifacts=work.artifact_payloads,
            )
            if capsule is not None:
                trace.record_reproducibility_capsule(capsule.to_payload())
        except Exception:
            return

    def _record_project_memory(
        self,
        *,
        project: str,
        session_id: str,
        task: str,
        files: tuple[str, ...],
        receipt: str,
        checks: tuple[object, ...],
    ) -> None:
        if self.knowledge_store is None:
            return
        try:
            brief = KnowledgeBriefBuilder(self.knowledge_store).build_for_session(session_id)
            sources = [brief.synthesis_id] if brief.synthesis_id else []
            impl = KnowledgeNote.create(
                type="implementation",
                title=task[:120] or "Project implementation",
                body=(
                    "Implemented project task.\n\n"
                    f"Files changed:\n{_bullet_lines(files)}\n\n"
                    f"Receipt:\n{receipt}"
                ),
                tags=["project", "implementation", f"session:{session_id}"],
                sources=sources,
                session_id=session_id,
                project=str(Path(project).expanduser().resolve()),
            )
            self.knowledge_store.write_note(impl)
            if checks:
                verification = KnowledgeNote.create(
                    type="verification",
                    title=f"Verification for {task[:80] or 'project task'}",
                    body="Successful checks:\n" + _bullet_lines(
                        tuple(f"{item.command} (cwd {item.cwd})" for item in checks)
                    ),
                    tags=["project", "verification", f"session:{session_id}"],
                    sources=[impl.id],
                    session_id=session_id,
                    project=str(Path(project).expanduser().resolve()),
                )
                self.knowledge_store.write_note(verification)
                self.knowledge_store.link(impl.id, verification.id, "verifies")
            if brief.synthesis_id:
                self.knowledge_store.link(brief.synthesis_id, impl.id, "implements")
        except (OSError, ValueError):
            return
