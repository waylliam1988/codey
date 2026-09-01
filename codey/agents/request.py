"""Typed input for one coding-agent run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from codey.agents.handoff import ConversationContext
from codey.agents.tools import AgentToolFns
from codey.completion.verification_policy import VerificationCandidate
from codey.providers import ChatProvider
from codey.protocols import ProtocolCodec
from codey.runtime.events import RunEvent, print_run_event
from codey.runtime.models import ToolCall
from codey.toolchain.runtime import ToolOutcome

DEFAULT_MAX_TURNS = 50
DEFAULT_STAGNANT_TURNS = 4


class ChangeTracker(Protocol):
    def capture_before(self, rel: str) -> None:
        """Record a file's pre-write content if it has not been captured."""

    def capture_after(self, rel: str) -> None:
        """Record the content produced by a successful write."""


@dataclass(frozen=True)
class RecoveredToolOutcome:
    effect_id: str
    call: ToolCall
    outcome: ToolOutcome
    turn: int
    tool_index: int


@dataclass(frozen=True)
class AgentRequest:

    provider: ChatProvider
    project: Path
    task: str
    codec: ProtocolCodec | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    stagnant_turns: int = DEFAULT_STAGNANT_TURNS
    on_event: Callable[[RunEvent], None] = print_run_event
    on_shell_request: Callable[[str, str], None] | None = None
    stop_flag: Any = None
    fresh_chat: bool = True
    strict_fresh_chat: bool = False
    change_tracker: ChangeTracker | None = None
    conversation: ConversationContext | None = None
    provider_id: str = ""
    handoff: str = ""
    project_facts: str = ""
    research_context: str = ""
    project_map: str = ""
    project_config_warnings: str = ""
    work_checkpoint: str = ""
    verification_candidates: tuple[VerificationCandidate, ...] = ()
    verification_candidate_loader: Callable[
        [], tuple[VerificationCandidate, ...]
    ] | None = None
    verification_changed_files: tuple[str, ...] = ()
    verification_successful_checks: tuple[VerificationCandidate, ...] = ()
    coding_context_enabled: bool = True
    ghost_directive: str = ""
    ghost_continuity: str = ""
    completion_repair_context: str = ""
    completion_repair_context_payload: dict[str, object] | None = None
    permission_profile: str = "coding_writer"
    tool_fns: AgentToolFns | None = None
    trace_recorder: Any = None
    session_id: str = ""
    run_id: str = ""
    runtime_effects: Any = None
    recovered_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()
__all__ = [
    "AgentRequest",
    "ChangeTracker",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_STAGNANT_TURNS",
    "RecoveredToolOutcome",
]
