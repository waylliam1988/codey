"""Dependencies and outward result of the single operation driver."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from codey.operations.generation_boundary import GenerationBoundaryRequest, GenerationBoundaryResult
from codey.operations.kernel_protocol import ReceivedProviderTurn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.workspace.context_admission import GhostContextSource


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: Any | None = None
    delivery: str = "not_required"

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
    delivered: Mapping[str, ToolResult] | None = None
    start_turn: int | None = None
    initial_results: list[ToolResult] | None = None
    provider_session_changed: bool = False
    initial_turn: ReceivedProviderTurn | None = None
    window_ref: Any = None
    replayed_result_ids: frozenset[str] = frozenset()

@dataclass(frozen=True)
class KernelExecutionDeps:
    """Tool, project, and workspace resources for one kernel run."""

    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None
    project_path: Any = None
    tool_fns: Any = None
    research_tools: Any = None
    change_tracker: Any = None
    managed_outputs: Any = None
    session_id: str = ""
    permission_profile: str = "coding_writer"
    workspace_ignored_paths: Any = ()
    workspace_revision_store: Any = None
    ensure_workspace_writer: Callable[[], None] | None = None
    durable: Any = None

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


