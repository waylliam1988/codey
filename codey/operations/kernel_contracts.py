"""Dependencies and outward result of the single operation driver."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict

from codey.operations.generation_boundary import GenerationBoundaryRequest, GenerationBoundaryResult
from codey.providers.attempt_gate import AttemptGateCallback
from codey.providers.transport import ProviderTransport
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.questions import QuestionPayload
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)
from codey.workspace.context_admission import GhostContextSource

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.shell_approval import ShellApprovalRequest
    from codey.agents.tools import AgentToolFns
    from codey.completion.contract import CompletionProof
    from codey.operations.completion_context import CompletionContextInput
    from codey.operations.task_effects import KernelEffectSink
    from codey.research.finding_report import ResearchReport
    from codey.research.tools import ResearchTools
    from codey.runs.trace import RunTraceRecorder
    from codey.runtime.core.protocol_window import ProtocolWindowRef
    from codey.runtime.observe.events import RunEvent
    from codey.workspace.revision import WorkspaceRevisionStore


ToolExecutor = Callable[[ToolCall], ToolResult]
DeliveryStatus = Literal["not_required", "success", "failed", "unknown"]


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


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
    research_report: ResearchReport | None = None

    def to_payload(self) -> KernelResultPayload:
        return {"schema_version": 2, "completed": self.completed, "summary": self.summary,
                "turns": self.turns, "stop_reason": self.stop_reason, "delivery": self.delivery,
                "proof": decode_json_object(self.proof.to_payload()) if self.proof is not None else None,
                "research_report": decode_json_object(self.research_report.to_payload()) if self.research_report is not None else None}


def result_from_payload(payload: object) -> KernelResult:
    from codey.completion.contract import _proof_from_payload

    legacy = {"completed", "summary", "turns", "stop_reason", "proof", "delivery"}
    current = legacy | {"research_report"}
    if not isinstance(payload, dict):
        raise ValueError("invalid persisted kernel result")
    if set(payload) == legacy:
        payload = {**payload, "research_report": None}
    elif set(payload) == current | {"schema_version"}:
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 2:
            raise ValueError("unknown persisted kernel result version")
        payload = {key: value for key, value in payload.items() if key != "schema_version"}
    payload = decode_json_object(payload)
    if set(payload) != current:
        raise ValueError("invalid persisted kernel result")
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
    from codey.research.finding_report import FindingReport

    report = payload["research_report"]
    compiled = FindingReport.from_payload(report) if report is not None else None
    if compiled is not None and (not completed or compiled.text != summary):
        raise ValueError("accepted research report differs from kernel delivery")
    return KernelResult(completed, summary, turns, stop_reason, proof=proof, delivery=delivery, research_report=compiled)


class RetryDecision(TypedDict):
    purpose: Literal["retry"]
    prompt: str
    error: str
    # Only schema v1 decisions carry a pending observation; new writes use the checkpoint.
    counters: NotRequired[dict[str, object]]
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
    ghost_sources: tuple[GhostContextSource, ...] = ()
    admit_hints: bool = True
    attempt_gate: AttemptGateCallback | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: StopSignal | None = None
    stagnant_turns: int | None = None
    window_ref: ProtocolWindowRef | None = None

    def __post_init__(self) -> None:
        from codey.operations.provider_session import adapt_provider

        object.__setattr__(self, "provider", adapt_provider(self.provider))


@dataclass(frozen=True)
class KernelExecutionDeps:
    """Tool, project, and workspace resources for one kernel run."""

    executors: Mapping[str, ToolExecutor] | None = None
    # Durable intent submission is an execution prerequisite. The observation
    # location remains temporarily accepted for compatibility with older
    # callers; production builders should populate this field.
    intent_sink: KernelEffectSink | None = None
    project_path: Path | None = None
    tool_fns: AgentToolFns | None = None
    research_tools: ResearchTools | None = None
    change_tracker: ChangeTracker | None = None
    permission_profile: str = "coding_writer"
    workspace_ignored_paths: tuple[str, ...] = ()
    workspace_revision_store: WorkspaceRevisionStore | None = None
    ensure_workspace_writer: Callable[[], None] | None = None

@dataclass(frozen=True)
class KernelObservationDeps:
    """Completion, recovery, and event sinks for one kernel run."""

    intent_sink: KernelEffectSink | None = None
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

    def __post_init__(self) -> None:
        # Legacy construction is normalized once. Execution owns the sink;
        # the observation record held by a run never retains a second owner.
        if self.observation.intent_sink is not None:
            object.__setattr__(self, "execution", replace(self.execution, intent_sink=self.observation.intent_sink))
            object.__setattr__(self, "observation", replace(self.observation, intent_sink=None))
