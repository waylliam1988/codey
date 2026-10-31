"""Dependencies and outward result of the single operation driver."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

from codey.operations.generation_boundary import GenerationBoundaryRequest, GenerationBoundaryResult
from codey.runtime.core.models import ToolCall
from codey.workspace.context_admission import GhostContextSource

if TYPE_CHECKING:
    from codey.completion.contract import CompletionProof


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: CompletionProof | None = None
    delivery: str = "not_required"
    research_report: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        from codey.providers.response_record import json_value

        return cast(dict[str, Any], json_value(self))

def result_from_payload(payload: Any) -> KernelResult:
    from codey.completion.contract import _proof_from_payload

    if (not isinstance(payload, dict) or set(payload) != {"completed", "summary", "turns", "stop_reason", "proof", "delivery", "research_report"}
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
    result: dict[str, Any]
    workspace_revision: int
    workspace_fingerprint: str


class QuestionDecision(TypedDict):
    purpose: Literal["question"]
    question: dict[str, Any]
    question_id: str
    result: dict[str, Any]


class ClosingDecision(TypedDict):
    purpose: Literal["closing"]
    result: dict[str, Any]


KernelDecision = RetryDecision | DoneDecision | QuestionDecision | ClosingDecision


@dataclass(frozen=True)
class KernelTransportDeps:
    """Provider and turn transport state owned by the kernel boundary."""

    provider: Any
    run_id: str = ""
    effect_scope: str = ""
    provider_id: object = ""
    user_task: object = ""
    context_text: str = ""
    # Logical request authority, independent of an automatically allocated
    # execution workspace. Unscoped standalone requests use "request".
    context_scope: str = "request"
    ghost_sources: tuple[GhostContextSource, ...] = ()
    admit_hints: bool = True
    attempt_gate: Callable[[Any, dict[str, Any]], None] | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: Any = None
    stagnant_turns: int | None = None
    window_ref: Any = None

@dataclass(frozen=True)
class KernelExecutionDeps:
    """Tool, project, and workspace resources for one kernel run."""

    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None
    project_path: Any = None
    tool_fns: Any = None
    research_tools: Any = None
    change_tracker: Any = None
    permission_profile: str = "coding_writer"
    workspace_ignored_paths: Any = ()
    workspace_revision_store: Any = None
    ensure_workspace_writer: Callable[[], None] | None = None

@dataclass(frozen=True)
class KernelObservationDeps:
    """Completion, recovery, and event sinks for one kernel run."""

    intent_sink: Any = None
    completion_context: Any = None
    on_event: Callable[[Any], None] | None = None
    on_shell_request: Callable[[Any], None] | None = None
    trace_recorder: Any = None
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


