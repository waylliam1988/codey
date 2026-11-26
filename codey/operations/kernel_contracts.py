"""Dependencies and outward result of the single operation driver."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict

from codey.memory.contracts import MemoryTrace
from codey.operations.generation_boundary import GenerationBoundaryRequest, GenerationBoundaryResult
from codey.operations.kernel.executor import ToolExecutor as ToolExecutor
from codey.providers.attempt_gate import AttemptGateCallback
from codey.providers.transport import ProviderTransport
from codey.task.questions import QuestionPayload
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)
from codey.workspace.context_admission import ContextCandidate

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.shell_approval import ShellApprovalRequest
    from codey.agents.tools import AgentToolFns
    from codey.automation.browser_session import BrowserSession
    from codey.completion.contract import CompletionProof
    from codey.operations.completion_context import CompletionContextInput
    from codey.operations.task_effects import KernelEffectSink
    from codey.research.answer_report import AnswerReport
    from codey.research.tools import ResearchTools
    from codey.runs.trace import RunTraceRecorder
    from codey.runtime.observe.events import RunEvent
    from codey.types.protocol_window import ProtocolWindowRef
    from codey.workspace.revision import WorkspaceRevisionStore


DeliveryStatus = Literal["not_required", "success", "failed", "unknown"]


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


KERNEL_RESULT_SCHEMA_VERSION = 2


class KernelResultPayload(TypedDict):
    schema_version: int
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: JsonObject | None
    delivery: DeliveryStatus
    research_report: JsonObject | None


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: CompletionProof | None = None
    delivery: DeliveryStatus = "not_required"
    research_report: AnswerReport | None = None

    def to_payload(self) -> KernelResultPayload:
        return {"schema_version": KERNEL_RESULT_SCHEMA_VERSION, "completed": self.completed, "summary": self.summary,
                "turns": self.turns, "stop_reason": self.stop_reason, "delivery": self.delivery,
                "proof": self.proof.to_payload() if self.proof is not None else None,
                "research_report": self.research_report.to_payload() if self.research_report is not None else None}


def result_from_payload(payload: object) -> KernelResult:
    from codey.completion.contract import _proof_from_payload

    payload = decode_json_object(payload)
    if set(payload) != KernelResultPayload.__required_keys__:
        raise ValueError("invalid persisted kernel result")
    if require_json_integer(payload["schema_version"], "kernel result version") != KERNEL_RESULT_SCHEMA_VERSION:
        raise ValueError("unknown persisted kernel result version")
    completed = require_json_boolean(payload["completed"], "completed")
    summary = require_json_string(payload["summary"], "summary")
    turns = require_json_integer(payload["turns"], "turns")
    stop_reason = require_json_string(payload["stop_reason"], "stop_reason")
    deliveries: dict[str, DeliveryStatus] = {"not_required": "not_required", "success": "success",
                                           "failed": "failed", "unknown": "unknown"}
    delivery = deliveries.get(require_json_string(payload["delivery"], "delivery"))
    if turns < 0 or delivery is None:
        raise ValueError("invalid persisted kernel result")
    raw_proof = payload["proof"]
    proof = _proof_from_payload(dict[str, object](decode_json_object(raw_proof))) if raw_proof is not None else None
    if raw_proof is not None and proof is None:
        raise ValueError("invalid persisted completion proof")
    from codey.research.answer_report import AnswerReport

    report = payload["research_report"]
    compiled = AnswerReport.from_payload(report) if report is not None else None
    if compiled is not None:
        from codey.research.report_values import has_project_delivery, report_delivery_matches

        combined = has_project_delivery(proof)
        if not completed or not report_delivery_matches(summary, compiled.text, combined=combined):
            raise ValueError("accepted research report differs from kernel delivery")
    return KernelResult(completed, summary, turns, stop_reason, proof=proof, delivery=delivery, research_report=compiled)


class RetryDecision(TypedDict):
    purpose: Literal["retry"]
    prompt: str
    error: str
    error_source: NotRequired[str]
    error_type: NotRequired[str]


class DoneDecision(TypedDict):
    purpose: Literal["done"]
    result: KernelResultPayload
    workspace_revision: int
    workspace_fingerprint: str


class QuestionDecision(TypedDict):
    purpose: Literal["question"]
    question: QuestionPayload
    question_id: str
    result: KernelResultPayload


class ClosingDecision(TypedDict):
    purpose: Literal["closing"]
    result: KernelResultPayload


KernelDecision = RetryDecision | DoneDecision | QuestionDecision | ClosingDecision


@dataclass(frozen=True)
class KernelTransportDeps:
    """Provider and turn transport state owned by the kernel boundary."""

    provider: ProviderTransport
    run_id: str = ""
    effect_scope: str = ""
    provider_id: str = ""
    user_task: str = ""
    context_text: str = ""
    # Logical request authority, independent of an automatically allocated
    # execution workspace. Unscoped standalone requests use "request".
    context_scope: str = "request"
    memory_sources: tuple[ContextCandidate, ...] = ()
    memory_traces: tuple[MemoryTrace, ...] = ()
    collect_memory_candidates: bool = False
    admit_hints: bool = True
    attempt_gate: AttemptGateCallback | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: StopSignal | None = None
    stagnant_turns: int | None = None
    window_ref: ProtocolWindowRef | None = None
    attempt_validate_memory_sources: Callable[[tuple[ContextCandidate, ...]], bool] | None = None

    def __post_init__(self) -> None:
        from codey.operations.provider_session import adapt_provider

        object.__setattr__(self, "provider", adapt_provider(self.provider))


@dataclass(frozen=True)
class KernelExecutionDeps:
    """Tool, project, and workspace resources for one kernel run."""

    executors: Mapping[str, ToolExecutor] | None = None
    intent_sink: KernelEffectSink | None = None
    project_path: Path | None = None
    tool_fns: AgentToolFns | None = None
    research_tools: ResearchTools | None = None
    browser_session: BrowserSession | None = None
    change_tracker: ChangeTracker | None = None
    permission_profile: str = "coding_writer"
    workspace_ignored_paths: tuple[str, ...] = ()
    workspace_revision_store: WorkspaceRevisionStore | None = None
    ensure_workspace_writer: Callable[[], None] | None = None

@dataclass(frozen=True)
class KernelObservationDeps:
    """Completion, recovery, and event sinks for one kernel run."""

    completion_context: CompletionContextInput | None = None
    on_event: Callable[[RunEvent], None] | None = None
    on_shell_request: Callable[[ShellApprovalRequest], None] | None = None
    trace_recorder: RunTraceRecorder | None = None
    propagate_provider_failure: bool = False
    # Called after a completed tool round or before final task handoff.  The
    # callback owns durable inbox placement; its returned text is appended to
    # the next text-provider prompt.  Native providers keep their protocol
    # receipt chain intact and therefore consume the durable placement on the
    # following generation boundary.
    on_boundary: Callable[[GenerationBoundaryRequest], GenerationBoundaryResult] | None = None

@dataclass(frozen=True)
class KernelRunRequest:
    """Single dependency boundary for the shared task kernel."""

    transport: KernelTransportDeps
    execution: KernelExecutionDeps = KernelExecutionDeps()
    observation: KernelObservationDeps = KernelObservationDeps()
    task_guidance: str = ""
