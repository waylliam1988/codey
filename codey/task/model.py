"""Task-facing data model.

This module is the boundary between user-visible task submission protocols and
the internal runtime.  It deliberately contains no provider, server, Ghost, or
tool execution imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from codey.types.json_value import decode_json_object, require_json_boolean, require_json_integer, require_json_string

TaskKind = Literal["chat", "project", "research", "review", "planning"]


@dataclass(frozen=True)
class StartTask:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("task text is required")


@dataclass(frozen=True)
class EditUserMessage:
    entry_id: str
    text: str
    expected_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise ValueError("entry_id is required")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("task text is required")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")


@dataclass(frozen=True)
class ResumeRun:
    source_run_id: str
    instruction: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_run_id, str) or not self.source_run_id.strip():
            raise ValueError("source_run_id is required")


@dataclass(frozen=True)
class HandoffRun:
    source_run_id: str
    instruction: str
    observe_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source_run_id, str) or not self.source_run_id.strip():
            raise ValueError("source_run_id is required")
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("handoff instruction is required")


class ModelHandoffRejected(ValueError):
    pass


@dataclass(frozen=True)
class ResolveShellApproval:
    approval_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, str) or not self.approval_id.strip():
            raise ValueError("approval_id is required")


TaskInput = StartTask | EditUserMessage | ResumeRun | HandoffRun | ResolveShellApproval


@dataclass(frozen=True)
class TaskSubmission:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    intent: str = "auto"
    run_id: str = ""
    # User-authorized capabilities for this submission (e.g. ("web.read",)).
    # Recorded at the entry boundary from explicit user intent (task text,
    # Research button, UI toggles). Never derived from model output.
    requested_capabilities: tuple[str, ...] = ()
    # Strict Research was explicitly enabled for this submission.
    strict_research: bool = False
    # Explicit completion requirement from the task entry. The completion
    # gate never infers this from keywords or write permission; read-only
    # tasks keep False even when the policy still grants project.write.
    project_changes_required: bool = False
    sources_open_required: bool = False
    # Entry-generated explicit denials (e.g.明确只读 → project.write/shell.approval).
    # build_task_policy() 统一扣除，模型输出与 requested 永远不能加回。
    denied_capabilities: tuple[str, ...] = ()
    previous_run_id: str = ""
    # Explicit review reuse source: prior completed run id whose structured
    # review may be reused when every identity and scope condition matches.
    # Empty means fresh review; never a file path.
    review_source_run_id: str = ""
    model_selection: dict[str, object] = field(default_factory=dict)
    # Canonical structured entry.  Legacy scalar fields above are retained at
    # the outer scheduling boundary while all new callers can pass one typed
    # input object and avoid ambiguous boolean combinations.
    input: TaskInput | None = None
    output_format: str = "text"
    output_schema: str = ""
    required_tools: tuple[str, ...] = ()

    @classmethod
    def from_input(
        cls,
        *,
        session_id: str,
        project: str | None,
        max_turns: int,
        provider_id: str,
        input: TaskInput,
        intent: str = "auto",
        run_id: str = "",
        requested_capabilities: tuple[str, ...] = (),
        strict_research: bool = False,
        project_changes_required: bool = False,
        sources_open_required: bool = False,
        denied_capabilities: tuple[str, ...] = (),
        review_source_run_id: str = "",
        model_selection: dict[str, object] | None = None,
        output_format: str = "text", output_schema: str = "",
        required_tools: tuple[str, ...] = (),
    ) -> TaskSubmission:
        if isinstance(input, (StartTask, EditUserMessage)):
            task = input.text
        elif isinstance(input, HandoffRun):
            task = input.instruction
        elif isinstance(input, ResumeRun):
            task = input.instruction or "Continue the source run"
        else:
            task = "Resolve the shell approval"
        return cls(
            session_id=session_id,
            project=project,
            task=task,
            max_turns=max_turns,
            continue_task=isinstance(input, (ResumeRun, ResolveShellApproval)),
            provider_id=provider_id,
            intent=intent,
            run_id=run_id,
            requested_capabilities=requested_capabilities,
            strict_research=strict_research,
            project_changes_required=project_changes_required,
            sources_open_required=sources_open_required,
            denied_capabilities=denied_capabilities,
            previous_run_id=input.source_run_id if isinstance(input, ResumeRun) else "",
            review_source_run_id=review_source_run_id,
            model_selection=dict(model_selection or {}),
            input=input,
            output_format=output_format, output_schema=output_schema,
            required_tools=required_tools,
        )

    def resolved_input(self) -> TaskInput:
        """Resolve external submission parameters before admission."""
        if self.input is not None:
            return self.input
        if self.continue_task and (self.previous_run_id or self.run_id):
            return ResumeRun(self.previous_run_id or self.run_id, self.task)
        return StartTask(self.task)

def task_input_payload(value: TaskInput) -> dict[str, object]:
    if isinstance(value, EditUserMessage):
        return {"kind": "edit_user_message", "entry_id": value.entry_id, "text": value.text,
                "expected_revision": value.expected_revision}
    if isinstance(value, StartTask):
        return {"kind": "start_task", "text": value.text}
    if isinstance(value, ResumeRun):
        return {"kind": "resume_run", "source_run_id": value.source_run_id, "instruction": value.instruction}
    if isinstance(value, HandoffRun):
        return {"kind": "handoff_run", "source_run_id": value.source_run_id, "instruction": value.instruction,
                "observe_only": value.observe_only}
    return {"kind": "resolve_shell_approval", "approval_id": value.approval_id}


def task_input_from_payload(value: object) -> TaskInput:
    row = decode_json_object(value)
    kind = row.get("kind")
    if kind == "edit_user_message" and set(row) == {"kind", "entry_id", "text", "expected_revision"}:
        return EditUserMessage(require_json_string(row["entry_id"], "entry_id"), require_json_string(row["text"], "text"),
                               require_json_integer(row["expected_revision"], "expected_revision"))
    if kind == "start_task" and set(row) == {"kind", "text"}:
        return StartTask(require_json_string(row["text"], "text"))
    if kind == "resume_run" and set(row) == {"kind", "source_run_id", "instruction"}:
        return ResumeRun(require_json_string(row["source_run_id"], "source_run_id"),
                         require_json_string(row["instruction"], "instruction"))
    if kind == "handoff_run" and set(row) == {"kind", "source_run_id", "instruction", "observe_only"}:
        return HandoffRun(require_json_string(row["source_run_id"], "source_run_id"),
                          require_json_string(row["instruction"], "instruction"),
                          require_json_boolean(row["observe_only"], "observe_only"))
    if kind == "resolve_shell_approval" and set(row) == {"kind", "approval_id"}:
        return ResolveShellApproval(require_json_string(row["approval_id"], "approval_id"))
    raise ValueError(f"invalid durable task input: {kind!r}")


def task_submission_state(request: TaskSubmission, *, task_kind: str) -> dict[str, object]:
    """Freeze the same dispatch facts at every durable task entry."""
    return {
        "project": request.project,
        "task": request.task,
        "max_turns": request.max_turns,
        "continue_task": request.continue_task,
        "provider_id": request.provider_id,
        "intent": request.intent,
        "task_kind": task_kind,
        "requested_capabilities": list(request.requested_capabilities),
        "strict_research": request.strict_research,
        "sources_open_required": request.sources_open_required,
        "project_changes_required": request.project_changes_required,
        "required_tools": list(request.required_tools),
        "denied_capabilities": list(request.denied_capabilities),
        "previous_run_id": request.previous_run_id,
        "review_source_run_id": request.review_source_run_id,
        "model_selection": dict(request.model_selection),
        "input": task_input_payload(request.resolved_input()),
        "output_format": request.output_format,
        "output_schema": request.output_schema,
    }


def derive_project_changes_required(
    body: Mapping[str, object] | None,
    *,
    intent: str = "",
    project: str | None = None,
) -> bool:
    """Explicit entry decision: does this task require project modification?

    Write permission (can) never implies must. Only project/hybrid kinds
    with an attached project default to must-change; every other intent
    (chat/research/planning/readonly/review/auto) defaults to False.
    An explicit boolean ``project_changes_required`` in the submission body
    always wins, so a read-only project task can stay completable without
    edits. Never keyword-guesses from task text.
    """
    try:
        data = body if isinstance(body, dict) else dict[str, object]()
        if isinstance(data, dict) and "project_changes_required" in data:
            raw = data.get("project_changes_required")
            if raw is True:
                return True
            if raw is False:
                return False
    except Exception:
        pass
    try:
        kind = (intent or "").strip().lower()
    except Exception:
        kind = ""
    try:
        has_project = bool((project or "").strip())
    except Exception:
        has_project = False
    if not has_project:
        return False
    # Only project and hybrid kinds contract to change files. Read-only
    # intents stay False even with a project attached.
    return kind in {"project", "hybrid"}
