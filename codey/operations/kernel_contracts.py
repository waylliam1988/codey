"""Dependencies and outward result of the single operation driver."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict, cast

from codey.operations.generation_boundary import GenerationBoundaryRequest, GenerationBoundaryResult
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.questions import QuestionPayload
from codey.workspace.context_admission import GhostContextSource

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.shell_approval import ShellApprovalRequest
    from codey.agents.tools import AgentToolFns
    from codey.completion.contract import CompletionProof
    from codey.operations.completion_context import CompletionContextInput
    from codey.operations.task_effects import KernelEffectSink
    from codey.runs.trace import RunTraceRecorder
    from codey.runtime.core.protocol_window import ProtocolWindowRef
    from codey.runtime.observe.events import RunEvent
    from codey.workspace.revision import WorkspaceRevisionStore


ToolExecutor = Callable[[ToolCall], ToolResult]
DeliveryStatus = Literal["not_required", "success", "failed", "unknown"]


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class KernelResultPayload(TypedDict):
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: dict[str, object] | None
    delivery: DeliveryStatus
    research_report: dict[str, object] | None


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: CompletionProof | None = None
    delivery: DeliveryStatus = "not_required"
    research_report: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        from codey.providers.response_record import json_value

        return {"schema_version": 2, **cast(dict[str, Any], json_value(self))}

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
    if (set(payload) != current
            or type(payload["completed"]) is not bool or not isinstance(payload["summary"], str)
            or type(payload["turns"]) is not int or payload["turns"] < 0
            or not isinstance(payload["stop_reason"], str)
            or payload["delivery"] not in {"not_required", "success", "failed", "unknown"}):
        raise ValueError("invalid persisted kernel result")
    proof = _proof_from_payload(payload["proof"]) if isinstance(payload["proof"], dict) else None
    if payload["proof"] is not None and proof is None:
        raise ValueError("invalid persisted completion proof")
    report = payload["research_report"]
    if report is not None:
        from codey.research.finding_report import FindingReport

        compiled = FindingReport.from_payload(report)
        if not payload["completed"] or compiled.text != payload["summary"]:
            raise ValueError("accepted research report differs from kernel delivery")
    return KernelResult(payload["completed"], payload["summary"], payload["turns"], payload["stop_reason"],
                        proof=proof, delivery=payload["delivery"], research_report=report)


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

    provider: Any
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
    attempt_gate: Callable[[Any, dict[str, Any]], None] | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: StopSignal | None = None
    stagnant_turns: int | None = None
    window_ref: ProtocolWindowRef | None = None

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
    research_tools: Any = None
    change_tracker: ChangeTracker | None = None
    permission_profile: str = "coding_writer"
    workspace_ignored_paths: tuple[str, ...] = ()
    workspace_revision_store: WorkspaceRevisionStore | None = None
    ensure_workspace_writer: Callable[[], None] | None = None

@dataclass(frozen=True)
class KernelObservationDeps:
    """Completion, recovery, and event sinks for one kernel run."""

    intent_sink: Any = None
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


