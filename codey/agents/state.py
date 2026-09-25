"""Mutable state for one coding-agent loop invocation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from codey.agents.context import ProjectInstruction
from codey.agents.handoff import ConversationSnapshot
from codey.agents.request import AgentRequest
from codey.agents.tools import AgentToolFns
from codey.completion.verification_policy import VerificationCandidate
from codey.policies.permissions import PermissionProfile
from codey.protocols import ProtocolCodec
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, RenderedPromptSection
from codey.workspace.context_source import RenderedContextSource


@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"  # done | stopped | max_turns | no_progress | protocol
    turns: int = 0
    checks_passed: bool = False
    changed: bool = False
    checks_ran: bool = False


@dataclass
class LoopProgress:
    changed_files: set[str]
    read_file_paths: set[str]
    known_file_paths: set[str]
    wrote_files: bool = False


@dataclass
class LoopVerification:
    paths: set[str]
    edit_epoch: int
    successful_checks: list[tuple[str, str, int]]
    attempts: list[tuple[str, str, int]]
    checks_ran: bool = False
    checks_passed: bool = False
    candidates_epoch: int | None = None
    default_reminded_epoch: int | None = None
    candidates: tuple[VerificationCandidate, ...] = ()


class SeenInfoLRU:
    """Bounded dedupe for information-tool outputs.

    Keys are ``(tool_name, path, sha256(model_text)[:24])`` so long model
    outputs never accumulate in memory. Insertion-order eviction keeps the
    working set small and cold-start free (no extra deps, no fallback).
    """

    def __init__(self, max_items: int = 256) -> None:
        self.max_items = max(1, int(max_items))
        self._order: dict[tuple[str, str, str], None] = {}

    def __contains__(self, key: object) -> bool:
        return key in self._order

    def __len__(self) -> int:
        return len(self._order)

    def add(self, key: tuple[str, str, str]) -> bool:
        if key in self._order:
            return False
        self._order[key] = None
        while len(self._order) > self.max_items:
            self._order.pop(next(iter(self._order)))
        return True

    def clear(self) -> None:
        self._order.clear()


def seen_info_key(tool_name: str, path: str, model_text: str) -> tuple[str, str, str]:
    import hashlib

    digest = hashlib.sha256(str(model_text or "").encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return (str(tool_name or ""), str(path or ""), digest)


@dataclass(frozen=True)
class ToolAttemptRecord:
    tool: str
    call_fp: str
    result_fp: str
    ok: bool
    changed: bool
    turn: int
    edit_epoch: int = 0


@dataclass
class LoopStagnation:
    seen_info: SeenInfoLRU = field(default_factory=SeenInfoLRU)
    count: int = 0
    attempts: deque[ToolAttemptRecord] = field(default_factory=lambda: deque(maxlen=24))


@dataclass(frozen=True, slots=True)
class ResolvedLoopConfig:
    """Values resolved once before the first provider send."""

    project: Path
    codec: ProtocolCodec
    profile: PermissionProfile
    tool_fns: AgentToolFns
    active_provider_id: str
    max_turns: int
    stagnant_turns: int
    system_prompt_text: str
    project_instructions: tuple[ProjectInstruction, ...]
    native_tools: tuple[dict[str, object], ...] | None
    verification_required: bool
    verification_forbidden: bool


@dataclass
class PendingPromptState:
    context_rows: list[RenderedContextSource] = field(default_factory=list)
    repair_sections: list[RenderedPromptSection] = field(default_factory=list)
    provider_send_index: int = 0


@dataclass
class AgentLoopSession:
    request: AgentRequest
    config: ResolvedLoopConfig
    trace: FailOpenPromptTrace
    progress: LoopProgress
    verification: LoopVerification
    stagnation: LoopStagnation
    prompt: PendingPromptState = field(default_factory=PendingPromptState)


def emit(session: AgentLoopSession, event: RunEvent) -> None:
    session.request.on_event(event)


def snapshot(
    session: AgentLoopSession,
    summary: str = "",
    blocker: str = "",
) -> ConversationSnapshot:
    prior = session.request.conversation.snapshot if session.request.conversation else None
    return ConversationSnapshot(
        mode="project",
        goal=(prior.goal if prior and prior.goal else session.request.task),
        project=str(session.config.project),
        provider_id=session.config.active_provider_id,
        changed_files=tuple(sorted(session.progress.changed_files)),
        checks_passed=session.verification.checks_passed,
        summary=summary,
        blocker=blocker,
        conversation_summary=(prior.conversation_summary if prior else ""),
    )


__all__ = [
    "AgentLoopSession",
    "LoopProgress",
    "LoopStagnation",
    "LoopVerification",
    "PendingPromptState",
    "ResolvedLoopConfig",
    "RunResult",
    "SeenInfoLRU",
    "ToolAttemptRecord",
    "emit",
    "seen_info_key",
    "snapshot",
]
