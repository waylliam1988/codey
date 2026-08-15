"""Provider-agnostic tool-using agent runtime.

Codey asks a chat provider for JSON tool calls and converts those calls into
local ToolCall objects. The runtime only depends on ChatProvider and
ProtocolCodec interfaces; browser automation and provider-specific selectors
stay outside this module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from codey import cancellation
from codey.coding_context import CodingContext, render_coding_context
from codey.context_source import (
    ContextSource,
    render_context_sources_with_metadata,
)
from codey.events import RunEvent, print_run_event
from codey.agent_tools import (
    DEFAULT_TOOL_FNS,
    AgentToolFns,
)
from codey.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_continuation_prompt,
)
from codey.models import ToolCall, ToolPlan, ToolResult
from codey.providers import ChatProvider
from codey.protocols import JsonToolCodec, ProtocolCodec
from codey.protocols.json_codec import (
    PROTOCOL_DISALLOWED_TOOL,
    PROTOCOL_DIRECT_ANSWER,
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_NATIVE_TOOL_DENIAL,
    PROTOCOL_NESTED_TOOL_IN_DONE,
    PROTOCOL_NO_JSON,
    PROTOCOL_UNKNOWN_TOOL,
    _balanced_json_objects,
)
from codey.permission_profiles import allows_context_source, profile_for_name
from codey.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelope,
    PromptEnvelopeSection,
)
from codey.tool_definition import (
    INFORMATION_RUNTIME_TOOL_NAMES,
    SUPPORTED_RUNTIME_TOOL_NAMES,
    render_tool_activity,
)
from codey.tool_runtime import (
    EditBlock,
    ToolOutcome,
    safe_join,
)
from codey.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    select_verification_candidate,
)
from codey.work_checkpoint import MAX_WORK_CHECKPOINT_PROMPT_CHARS

DEFAULT_MAX_TURNS = 50
DEFAULT_STAGNANT_TURNS = 4
SUPPORTED_TOOL_NAMES = SUPPORTED_RUNTIME_TOOL_NAMES
INFORMATION_TOOL_NAMES = INFORMATION_RUNTIME_TOOL_NAMES
PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_INSTRUCTION_CHARS = 12000
PROJECT_INSTRUCTIONS_CONTEXT_BUDGET = (
    MAX_PROJECT_INSTRUCTION_CHARS * len(PROJECT_INSTRUCTION_FILES) + 1000
)
VERIFIED_FACTS_CONTEXT_BUDGET = 8000
RESEARCH_CONTEXT_BUDGET = 7000
PROJECT_MAP_CONTEXT_BUDGET = 12000
PROJECT_CONFIG_WARNINGS_CONTEXT_BUDGET = 1200
WORK_CHECKPOINT_CONTEXT_BUDGET = MAX_WORK_CHECKPOINT_PROMPT_CHARS
INITIAL_LISTING_CONTEXT_BUDGET = 4000
CODING_CURRENT_CONTEXT_BUDGET = 3000
GHOST_DIRECTIVE_CONTEXT_BUDGET = 900
GHOST_CONTINUITY_CONTEXT_BUDGET = 900
VERIFICATION_REQUEST_RE = re.compile(
    r"\b("
    r"run|test|tests|unittest|pytest|verify|verification|check|build|lint|typecheck"
    r")\b|跑测试|运行测试|测试通过|验证|检查",
    re.IGNORECASE,
)
DEFAULT_CODEC = JsonToolCodec()


class ChangeTracker(Protocol):
    def capture_before(self, rel: str) -> None:
        """Record a file's pre-write content if it has not been captured."""

    def capture_after(self, rel: str) -> None:
        """Record the content produced by a successful write."""


@dataclass(frozen=True)
class ProjectInstruction:
    name: str
    content: str
    truncated: bool = False


def parse_reply(text: str, codec: ProtocolCodec = DEFAULT_CODEC) -> ToolPlan:
    return codec.parse(text)


# ----------------------------------------------------------------- tools ---
def load_project_instructions(
    root: Path,
    *,
    max_chars: int = MAX_PROJECT_INSTRUCTION_CHARS,
) -> list[ProjectInstruction]:
    docs: list[ProjectInstruction] = []
    for name in PROJECT_INSTRUCTION_FILES:
        path = safe_join(root, name)
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars].rstrip() + "\n\n[content truncated]"
        docs.append(ProjectInstruction(name=name, content=content, truncated=truncated))
    return docs


def format_project_instructions(docs: list[ProjectInstruction]) -> str:
    if not docs:
        return ""
    chunks = []
    for doc in docs:
        label = f"{doc.name} (truncated)" if doc.truncated else doc.name
        chunks.append(f"--- {label} ---\n{doc.content}")
    return "\n\n".join(chunks)


def _call_arg(call: ToolCall, name: str, default: str = "") -> str:
    value = call.args.get(name, default)
    if value is None:
        return default
    return str(value)


def _edit_blocks_from_call(call: ToolCall) -> list[EditBlock]:
    replacements = call.args.get("replacements")
    if not isinstance(replacements, list):
        return []
    blocks: list[EditBlock] = []
    for item in replacements:
        if not isinstance(item, dict):
            continue
        search = str(item.get("search") or "")
        replace = str(item.get("replace") or "")
        blocks.append(EditBlock(search, replace))
    return blocks


def _edit_has_content(call: ToolCall) -> bool:
    return "content" in call.args


def _canonical_project_path(root: Path, rel: str) -> str:
    path = safe_join(root, rel)
    return path.relative_to(root.resolve()).as_posix()


def _read_before_edit_outcome(
    root: Path,
    rel: str,
    known_file_paths: set[str],
) -> ToolOutcome | None:
    canonical = _canonical_project_path(root, rel)
    target = safe_join(root, canonical)
    if target.is_file() and canonical not in known_file_paths:
        return ToolOutcome.error(
            f"read_file required before editing existing file: {canonical}"
        )
    return None


def _task_requests_verification(task: str) -> bool:
    return bool(VERIFICATION_REQUEST_RE.search(task or ""))


def _verification_reminder(task: str) -> str:
    return (
        "The user asked for verification, and files were changed, but no "
        "successful run tool call has completed yet. Reply with exactly one "
        'JSON object that calls run now, such as '
        '{"tool":"run","args":{"command":"python -m unittest","path":"."}}. '
        "After the run result is green, call done. Original task:\n"
        f"{task}"
    )


def _default_verification_reminder(candidate: VerificationCandidate) -> str:
    call = json.dumps(
        {
            "tool": "run",
            "args": {"command": candidate.command, "path": candidate.cwd},
        },
        separators=(",", ":"),
    )
    return (
        "Files changed and a trusted local check is available. Run this check "
        "after the latest edit before completing:\n\n"
        f"{call}"
    )


def _protocol_repair_prompt(
    codec: ProtocolCodec,
    plan: ToolPlan,
    *,
    previous_reply: str = "",
) -> str:
    error = str(plan.protocol_error or "invalid JSON tool call")
    kind = str(plan.protocol_error_kind or "").strip()
    previous = _previous_tool_object(previous_reply, codec, kind, error)
    lines = [
        f"Protocol error: {_repair_error_summary(error, kind)}",
        "",
        "Preserve the previous intended path, content, old_string/new_string, "
        "command, and other valid arguments when possible; only fix the schema "
        "or tool-contract error.",
        "Examples below show schema only; do not replace valid previous values "
        "with placeholder or example values.",
        "",
    ]

    if kind == PROTOCOL_UNKNOWN_TOOL:
        tool = _unknown_tool_from_error(error)
        edit_example = _codec_public_example(codec, "edit")
        if tool in {"write", "write_file"} and edit_example:
            example = _unknown_write_repair_example(previous)
            lines.extend((
                "The previous reply used an unknown write tool. Coding has no "
                f"{tool} tool.",
                "Create a new file with edit(content=...), or modify an existing "
                "file with edit(old_string/new_string).",
            ))
            if example:
                lines.extend(("", "Example preserving your previous intent:", example))
        else:
            example = _preferred_read_example(codec)
            lines.extend((
                "The previous reply used an unknown local tool.",
                "Use only the coding JSON tools listed in the system prompt.",
            ))
            if example:
                lines.extend(("", "Example:", example))
    elif kind == PROTOCOL_DISALLOWED_TOOL:
        example = _preferred_read_example(codec) or _codec_public_example(codec, "done")
        lines.extend((
            "The previous reply used a tool that exists, but this current phase "
            "does not allow it.",
            "Use only the tools listed in the system prompt for this phase.",
        ))
        if example:
            lines.extend(("", "Example:", example))
    elif kind == PROTOCOL_INVALID_ARGS:
        lines.extend(_invalid_args_repair_lines(error, previous, codec))
    elif kind == PROTOCOL_DIRECT_ANSWER:
        lines.extend((
            "The previous reply answered in prose.",
            "Coding replies must be one local-runner JSON command.",
            "If the task is complete, put the final user-facing answer in done.summary.",
            "",
            "Example:",
            '{"tool":"done","args":{"summary":"finished"}}',
        ))
    elif kind == PROTOCOL_NATIVE_TOOL_DENIAL:
        lines.extend((
            "Ignore website-native tool availability messages.",
            "These are local-runner JSON commands; the local runner executes them after you reply.",
            "",
            "Example:",
            _preferred_read_example(codec),
        ))
    elif kind == PROTOCOL_NESTED_TOOL_IN_DONE:
        example = _nested_tool_repair_example(previous, codec)
        lines.extend((
            "done.summary cannot contain another JSON tool call.",
            "If you intended to run a tool, call that tool directly instead of wrapping it in done.",
        ))
        if example:
            lines.extend(("", "Example preserving your previous intent:", example))
        else:
            lines.extend((
                "",
                "Example:",
                _codec_public_example(codec, "run") or _preferred_read_example(codec),
            ))
    elif kind == PROTOCOL_NO_JSON:
        example = _preferred_read_example(codec) or _codec_public_example(codec, "done")
        lines.extend((
            "Reply with a JSON tool call, not prose.",
        ))
        if example:
            lines.extend(("", "Example:", example))
    else:
        lines.append(codec.repair_prompt())

    lines.extend((
        "",
        "Reply with exactly one JSON object, no markdown fences and no other text.",
    ))
    return "\n".join(lines)


def _unknown_tool_from_error(error: str) -> str:
    match = re.search(r"unknown tool:\s*([A-Za-z0-9_-]+)", error)
    return match.group(1).strip().lower() if match else ""


def _repair_error_summary(error: str, kind: str) -> str:
    if kind == PROTOCOL_UNKNOWN_TOOL:
        tool = _unknown_tool_from_error(error)
        if tool:
            return f"unknown tool: {tool}"
    return error


def _invalid_args_repair_lines(
    error: str,
    previous: dict[str, object] | None = None,
    codec: ProtocolCodec | None = None,
) -> list[str]:
    folded = error.lower()
    if "positive integer" in folded and "offset" in folded:
        lines = [
            "read_file offset is 1-based and must be a positive integer.",
            "Use offset=1 or omit offset when reading from the beginning.",
            "Keep the same path and any valid limit from the previous read_file call.",
        ]
        example = _read_offset_repair_example(previous)
        if example:
            lines.extend(("", "Example preserving your previous intent:", example))
        return lines
    if "exactly one mode" in folded:
        lines = [
            "edit requires exactly one mode: content, old_string/new_string, or replacements.",
            "Use content only for a new file. For an existing file, use exact old_string/new_string.",
            "If old_string/new_string were already present, copy those strings exactly, including escaped \\n.",
        ]
        example = _edit_mode_repair_example(previous)
        if example:
            lines.extend(("", "Example preserving your previous intent:", example))
        return lines
    if "top-level path" in folded:
        return [
            "edit requires one top-level path and can only edit one file per call.",
            "",
            "Example:",
            '{"tool":"edit","args":{"path":"app.py","old_string":"old exact text","new_string":"new text"}}',
        ]
    if "read_files" in folded:
        return [
            "read_files requires a non-empty paths list.",
            "",
            "Example:",
            _codec_public_example(codec, "read_files"),
        ]
    if "parallel" in folded:
        return [
            "parallel accepts only read-only list_dir, read_file, and grep calls.",
            "",
            "Example:",
            _codec_public_example(codec, "parallel"),
        ]
    if "grep requires a query" in folded:
        return [
            "grep requires a non-empty query.",
            "",
            "Example:",
            _codec_public_example(codec, "grep"),
        ]
    if "find_references requires a symbol" in folded:
        return [
            "find_references requires a symbol.",
            "",
            "Example:",
            _codec_public_example(codec, "find_references"),
        ]
    if "requires a command" in folded:
        example = _codec_public_example(codec, "run") or _codec_public_example(codec, "shell")
        return [
            "run and shell require a command string.",
            "",
            "Example:",
            example,
        ]
    return [
        "The tool name was recognized, but its arguments do not match the coding schema.",
        "Use one valid JSON shape from the tool contract.",
        "",
        "Example:",
        _preferred_read_example(codec),
    ]


def _codec_public_example(codec: ProtocolCodec | None, tool_name: str) -> str:
    getter = getattr(codec, "public_example", None)
    if callable(getter):
        try:
            return str(getter(tool_name) or "")
        except (TypeError, ValueError):
            return ""
    return ""


def _preferred_read_example(codec: ProtocolCodec | None) -> str:
    for tool_name in ("read_file", "list_dir", "grep"):
        example = _codec_public_example(codec, tool_name)
        if example:
            return example
    return ""


def _previous_tool_object(
    previous_reply: str,
    codec: ProtocolCodec,
    kind: str,
    error: str,
) -> dict[str, object] | None:
    objects = _balanced_json_objects(previous_reply)
    if kind == PROTOCOL_UNKNOWN_TOOL:
        target_tool = _unknown_tool_from_error(error)
        if target_tool:
            for obj in objects:
                tool = str(obj.get("tool") or obj.get("name") or "").strip().lower()
                if tool == target_tool:
                    return dict(obj)
    if kind in {
        PROTOCOL_INVALID_ARGS,
        PROTOCOL_NESTED_TOOL_IN_DONE,
        PROTOCOL_DISALLOWED_TOOL,
    }:
        for obj in objects:
            tool = str(obj.get("tool") or obj.get("name") or "").strip()
            if not tool:
                continue
            plan = codec.parse(_json_example(dict(obj)))
            if plan.protocol_error_kind == kind:
                return dict(obj)
    for obj in objects:
        tool = str(obj.get("tool") or obj.get("name") or "").strip()
        if tool:
            return dict(obj)
    return None


def _previous_args(previous: dict[str, object] | None) -> dict[str, object]:
    if not previous:
        return {}
    args = previous.get("args")
    if isinstance(args, dict):
        return dict(args)
    return {
        str(key): value
        for key, value in previous.items()
        if key not in {"tool", "name"}
    }


def _json_example(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _unknown_write_repair_example(previous: dict[str, object] | None) -> str:
    args = _previous_args(previous)
    path = str(args.get("path") or "").strip()
    if not path:
        return '{"tool":"edit","args":{"path":"new_app.py","content":"..."}}'
    fixed_args: dict[str, object] = {"path": path}
    if "content" in args:
        fixed_args["content"] = str(args.get("content") or "")
    elif (single := _single_edit_args(args)) is not None:
        fixed_args.update(single)
    else:
        fixed_args["content"] = "..."
    return _json_example({"tool": "edit", "args": fixed_args})


def _read_offset_repair_example(previous: dict[str, object] | None) -> str:
    args = _previous_args(previous)
    path = str(args.get("path") or args.get("cwd") or "").strip()
    if not path:
        return ""
    fixed_args: dict[str, object] = {"path": path, "offset": 1}
    limit = _positive_int_value(args.get("limit"))
    if limit is not None:
        fixed_args["limit"] = limit
    return _json_example({"tool": "read_file", "args": fixed_args})


def _edit_mode_repair_example(previous: dict[str, object] | None) -> str:
    args = _previous_args(previous)
    path = str(args.get("path") or "").strip()
    if not path:
        return ""
    fixed_args: dict[str, object] = {"path": path}
    replacements = args.get("replacements")
    if isinstance(replacements, list) and replacements:
        cleaned: list[dict[str, str]] = []
        for item in replacements:
            if not isinstance(item, dict):
                continue
            single = _single_edit_args(item)
            if single is not None:
                cleaned.append(single)
        if cleaned:
            fixed_args["replacements"] = cleaned
            return _json_example({"tool": "edit", "args": fixed_args})
    if (single := _single_edit_args(args)) is not None:
        fixed_args.update(single)
        return _json_example({"tool": "edit", "args": fixed_args})
    fixed_args["old_string"] = "old exact text\n"
    fixed_args["new_string"] = "new text\n"
    return _json_example({"tool": "edit", "args": fixed_args})


def _single_edit_args(args: dict[str, object]) -> dict[str, str] | None:
    old = args.get("old_string", args.get("search"))
    new = args.get("new_string", args.get("replace", args.get("replacement")))
    if old is None or new is None or not str(old):
        return None
    return {"old_string": str(old), "new_string": str(new)}


def _positive_int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        number = int(value)
        if number > 0:
            return number
    return None


def _nested_tool_repair_example(
    previous: dict[str, object] | None,
    codec: ProtocolCodec | None = None,
) -> str:
    tool = str((previous or {}).get("tool") or (previous or {}).get("name") or "")
    if tool.lower().strip() != "done":
        return ""
    summary = _previous_args(previous).get("summary")
    if not isinstance(summary, str):
        return ""
    try:
        nested = json.loads(summary)
    except json.JSONDecodeError:
        return ""
    if not isinstance(nested, dict):
        return ""
    nested_tool = str(nested.get("tool") or nested.get("name") or "").strip()
    if not nested_tool:
        return ""
    if codec is not None and not _codec_public_example(codec, nested_tool):
        return ""
    return _json_example(nested)


# ----------------------------------------------------------------- loop ---

@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"  # done | stopped | max_turns | no_progress | protocol
    turns: int = 0
    checks_passed: bool = False
    changed: bool = False
    checks_ran: bool = False


def run(
    provider: ChatProvider,
    project: Path,
    user_task: str,
    *,
    codec: ProtocolCodec | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    stagnant_turns: int = DEFAULT_STAGNANT_TURNS,
    on_event=print_run_event,
    on_shell_request=None,
    stop_flag=None,
    fresh_chat: bool = True,
    strict_fresh_chat: bool = False,
    change_tracker: ChangeTracker | None = None,
    conversation: ConversationContext | None = None,
    provider_id: str = "",
    handoff: str = "",
    project_facts: str = "",
    research_context: str = "",
    project_map: str = "",
    project_config_warnings: str = "",
    work_checkpoint: str = "",
    verification_candidates: tuple[VerificationCandidate, ...] = (),
    verification_candidate_loader: Callable[
        [], tuple[VerificationCandidate, ...]
    ] | None = None,
    verification_changed_files: tuple[str, ...] = (),
    verification_successful_checks: tuple[VerificationCandidate, ...] = (),
    coding_context_enabled: bool = True,
    ghost_directive: str = "",
    ghost_continuity: str = "",
    permission_profile: str = "coding_writer",
    tool_fns: AgentToolFns | None = None,
    trace_recorder=None,
) -> RunResult:
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    profile = profile_for_name(permission_profile)
    codec = codec or JsonToolCodec(permission_profile=profile.name)
    system_prompt_text = codec.system_prompt()
    tool_fns = tool_fns or DEFAULT_TOOL_FNS
    max_turns = max(1, int(max_turns or DEFAULT_MAX_TURNS))
    stagnant_turns = max(1, int(stagnant_turns or DEFAULT_STAGNANT_TURNS))
    seen_info: set[tuple[str, str, str]] = set()
    stagnant_count = 0
    wrote_files = False
    checks_passed = False
    checks_ran = False
    changed_files = set(conversation.snapshot.changed_files if conversation else ())
    changed_files.update(verification_changed_files)
    verification_paths = set(verification_changed_files)
    read_file_paths: set[str] = set()
    known_file_paths: set[str] = set()
    verification_required = _task_requests_verification(user_task)
    edit_epoch = 0
    successful_verifications = [
        (item.command, item.cwd, edit_epoch) for item in verification_successful_checks
    ]
    default_verification_reminded_epoch: int | None = None
    verification_candidates_epoch: int | None = None
    project_text = str(project)
    active_provider_id = provider_id or getattr(provider, "name", "")
    trace = FailOpenPromptTrace(trace_recorder)

    trace.call("record_permission_profile", profile.name, phase="writer")
    try:
        trace.call("record_tool_contract_hash", codec.model_tool_contract_hash(), phase="writer")
    except Exception:
        pass
    trace.record_section(PromptEnvelopeSection(
        name="coding_system_prompt",
        text=system_prompt_text,
        purpose="coding JSON tool protocol",
        freshness="run_start",
        source_refs=("protocol:json",),
    ))
    trace.record_section(PromptEnvelopeSection(
        name="user_task",
        text=user_task,
        purpose="current user request",
        freshness="run_start",
        source_refs=("request:user_task",),
    ))

    def emit(event: RunEvent) -> None:
        on_event(event)

    def report_reply(turn: int, reply_text: str, note: str = "") -> None:
        emit(RunEvent.turn_started(turn, reply_text, note))

    def snapshot(summary: str = "", blocker: str = "") -> ConversationSnapshot:
        prior = conversation.snapshot if conversation else None
        return ConversationSnapshot(
            mode="project",
            goal=(prior.goal if prior and prior.goal else user_task),
            project=project_text,
            provider_id=active_provider_id,
            changed_files=tuple(sorted(changed_files)),
            checks_passed=checks_passed,
            summary=summary,
            blocker=blocker,
            conversation_summary=(prior.conversation_summary if prior else ""),
        )

    def finish(summary: str, stop_reason: str, turns: int) -> RunResult:
        if conversation is not None:
            blocker = "" if stop_reason == "done" else summary
            conversation.update_snapshot(snapshot(summary, blocker))
        return RunResult(summary, stop_reason, turns, checks_passed, wrote_files, checks_ran)

    def open_fresh_chat() -> bool:
        emit(RunEvent.status(f"[agent] opening a fresh {provider.name} conversation"))
        try:
            provider.new_chat()
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            if strict_fresh_chat:
                raise
            emit(RunEvent.status(
                f"[agent] could not open new chat: {exc}; reusing current tab"
            ))
            return False
        return True

    project_instructions = load_project_instructions(project)
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        emit(RunEvent.info("loaded project instructions", names=names))

    def project_intro(
        request: str,
        factual_handoff: str = "",
        *,
        include_ghost_directive: bool = True,
    ) -> str:
        if factual_handoff:
            trace.record_section(PromptEnvelopeSection(
                name="conversation_handoff",
                text=factual_handoff,
                purpose="bounded conversation handoff for a fresh provider window",
                freshness="run_start",
                source_refs=("conversation:handoff",),
            ))
        current = (
            render_continuation_prompt(factual_handoff, request)
            if factual_handoff
            else request
        )
        sources = []
        if include_ghost_directive:
            sources.append(
                ContextSource(
                    key="ghost_directive",
                    loader=lambda: ghost_directive,
                    budget=GHOST_DIRECTIVE_CONTEXT_BUDGET,
                    freshness="run_start",
                    why_included="bounded local confirmed Ghost memory",
                )
            )
            sources.append(
                ContextSource(
                    key="ghost_continuity",
                    loader=lambda: ghost_continuity,
                    budget=GHOST_CONTINUITY_CONTEXT_BUDGET,
                    freshness="run_start",
                    why_included="bounded local continuity projection",
                )
            )
        sources.extend((
            ContextSource(
                key="project_instructions",
                loader=lambda: format_project_instructions(project_instructions),
                budget=PROJECT_INSTRUCTIONS_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="project instruction files from the project root",
                heading="Project instructions:",
            ),
            ContextSource(
                key="verified_facts",
                loader=lambda: project_facts,
                budget=VERIFIED_FACTS_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="successful local checks recorded for this project",
                heading="Verified project facts from successful local runs:",
            ),
            ContextSource(
                key="research_brief",
                loader=lambda: research_context,
                budget=RESEARCH_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="bounded research brief from the current chat",
            ),
            ContextSource(
                key="project_map",
                loader=lambda: project_map,
                budget=PROJECT_MAP_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="bounded local project map prepared before writing",
            ),
            ContextSource(
                key="project_config_warnings",
                loader=lambda: project_config_warnings,
                budget=PROJECT_CONFIG_WARNINGS_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="bounded project-local config warnings",
                heading="Project config warnings:",
            ),
            ContextSource(
                key="work_checkpoint",
                loader=lambda: work_checkpoint,
                budget=WORK_CHECKPOINT_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="bounded local checkpoint from a previous project run",
            ),
            ContextSource(
                key="initial_listing",
                loader=lambda: tool_fns.list_directory(project, ".").model_text,
                budget=INITIAL_LISTING_CONTEXT_BUDGET,
                freshness="run_start",
                why_included="current top-level project listing",
                heading="Initial listing:",
            ),
        ))
        rendered_context = render_context_sources_with_metadata(
            source
            for source in sources
            if allows_context_source(profile, source.key)
        )
        trace.call("record_context_sources", rendered_context.sources)
        context = rendered_context.text
        context_block = f"{context}\n\n" if context else ""
        request_context = (
            "Project workspace: use paths relative to the project root.\n"
            f"{context_block}"
            f"User task:\n{current}"
        )
        rendered = PromptEnvelope((
            PromptEnvelopeSection(
                name="coding_system_prompt",
                text=system_prompt_text,
                purpose="coding JSON tool protocol",
                freshness="run_start",
                source_refs=("protocol:json",),
            ),
            PromptEnvelopeSection(
                name="coding_request_context",
                text=request_context,
                purpose="project workspace, bounded local context, and current request",
                freshness="run_start",
                source_refs=(
                    "project:workspace",
                    "request:user_task",
                    *(
                        f"context_source:{source.key}"
                        for source in rendered_context.sources
                    ),
                ),
                budget=sum(source.budget for source in rendered_context.sources),
                truncated=any(source.truncated for source in rendered_context.sources),
            ),
        )).render()
        trace.record_envelope(rendered)
        return rendered.text

    def send_prompt(
        prompt: str,
        *,
        restart_request: str | None = None,
        include_ghost_directive: bool = True,
    ) -> str:
        opened_fresh_chat = False
        if conversation is not None and conversation.needs_rollover(prompt):
            factual_handoff = conversation.prepare_model_handoff(provider.send)
            if open_fresh_chat():
                trace.record_section(PromptEnvelopeSection(
                    name="conversation_handoff",
                    text=factual_handoff,
                    purpose="bounded conversation handoff for provider rollover",
                    freshness="provider_rollover",
                    source_refs=("conversation:handoff",),
                ))
                prompt = project_intro(
                    restart_request or prompt,
                    factual_handoff,
                    include_ghost_directive=include_ghost_directive,
                )
                opened_fresh_chat = True
        trace.record_section(PromptEnvelopeSection(
            name="coding_outbound_prompt",
            text=prompt,
            purpose="coding prompt sent to provider",
            freshness="provider_send",
            source_refs=("provider_send:coding",),
        ))
        reply_text = provider.send(prompt)
        if conversation is not None:
            if opened_fresh_chat:
                conversation.begin_window(active_provider_id, "project", project_text)
            conversation.record_exchange(prompt, reply_text, snapshot())
        return reply_text

    def ensure_verification_candidates() -> tuple[VerificationCandidate, ...]:
        nonlocal verification_candidates
        nonlocal verification_candidates_epoch
        if (
            verification_candidate_loader is not None
            and verification_paths
            and verification_candidates_epoch != edit_epoch
        ):
            try:
                verification_candidates = verification_candidate_loader()
            except (OSError, TypeError, ValueError):
                verification_candidates = ()
            verification_candidates_epoch = edit_epoch
        return verification_candidates

    def selected_verification_candidate() -> VerificationCandidate | None:
        if not verification_paths:
            return None
        return select_verification_candidate(
            ensure_verification_candidates(),
            tuple(verification_paths),
        )

    def verification_is_fresh(candidate: VerificationCandidate | None) -> bool:
        return candidate is not None and any(
            epoch == edit_epoch
            and check_covers_selected_candidate(
                candidate,
                command,
                cwd,
                tuple(verification_paths),
            )
            for command, cwd, epoch in successful_verifications
        )

    def current_coding_context() -> str:
        if not coding_context_enabled:
            return ""
        candidate = selected_verification_candidate()
        return render_coding_context(
            CodingContext(
                read_files=tuple(sorted(read_file_paths)),
                edit_eligible_files=tuple(sorted(known_file_paths)),
                changed_files=tuple(sorted(verification_paths)),
                selected_verification=candidate,
                verification_fresh=verification_is_fresh(candidate),
            )
        )

    def append_coding_context(prompt: str) -> str:
        if not allows_context_source(profile, "coding_current_context"):
            return prompt
        rendered_context = render_context_sources_with_metadata(
            (
                ContextSource(
                    key="coding_current_context",
                    loader=current_coding_context,
                    budget=CODING_CURRENT_CONTEXT_BUDGET,
                    freshness="after_tool_result",
                    why_included="current read, edit, and verification facts",
                ),
            )
        )
        trace.call("record_context_sources", rendered_context.sources)
        context = rendered_context.text
        if not context:
            return prompt
        return f"{prompt}\n\n{context}"

    if fresh_chat:
        opened_fresh_chat = open_fresh_chat()
        intro = project_intro(user_task, handoff)
        trace.record_section(PromptEnvelopeSection(
            name="coding_outbound_prompt",
            text=intro,
            purpose="coding prompt sent to provider",
            freshness="provider_send",
            source_refs=("provider_send:coding",),
        ))
        reply = provider.send(intro)
        if conversation is not None:
            if opened_fresh_chat:
                conversation.begin_window(active_provider_id, "project", project_text)
            conversation.record_exchange(intro, reply, snapshot())
    elif conversation is not None:
        followup = (
            "Continue with the established project and JSON tool protocol.\n\n"
            f"User request:\n{user_task}"
        )
        reply = send_prompt(followup, restart_request=user_task)
    else:
        intro = project_intro(user_task)
        trace.record_section(PromptEnvelopeSection(
            name="coding_outbound_prompt",
            text=intro,
            purpose="coding prompt sent to provider",
            freshness="provider_send",
            source_refs=("provider_send:coding",),
        ))
        reply = provider.send(intro)
    report_reply(1, reply)

    for turn in range(1, max_turns + 1):
        if stop_flag is not None and stop_flag.is_set():
            emit(RunEvent.status("[agent] stopped by user."))
            return finish("stopped", "stopped", turn)
        plan = parse_reply(reply, codec)
        if plan.protocol_error:
            stagnant_count += 1
            emit(RunEvent.status(
                f"[agent] rejected invalid tool request: {plan.protocol_error}"
            ))
            if stagnant_count >= stagnant_turns:
                msg = f"stopped after {stagnant_turns} invalid tool requests"
                emit(RunEvent.status(f"[agent] {msg}."))
                return finish(msg, "protocol", turn)
            repair = _protocol_repair_prompt(codec, plan, previous_reply=reply)
            reply = send_prompt(
                repair,
                restart_request=repair,
                include_ghost_directive=False,
            )
            report_reply(turn + 1, reply, "(after protocol correction)")
            continue
        calls = plan.calls
        control = plan.control

        results: list[ToolResult] = []
        made_progress = False

        def record_tool_outcome(
            call: ToolCall,
            outcome: ToolOutcome,
            tool_index: int,
        ) -> None:
            nonlocal made_progress
            path = _call_arg(call, "path", ".")
            model_text = outcome.model_text
            emit(RunEvent.tool_finished(turn, call, outcome, index=tool_index))
            results.append(
                ToolResult(
                    call=call,
                    model_text=model_text,
                    truncated=outcome.truncated,
                    presentation=outcome.presentation,
                    audit=outcome.audit,
                    canonical=outcome.canonical,
                )
            )
            if call.name == "read" and outcome.ok:
                canonical = _canonical_project_path(project, path)
                read_file_paths.add(canonical)
                known_file_paths.add(canonical)
            produced_information = outcome.ok or outcome.exit_code is not None
            if call.name in INFORMATION_TOOL_NAMES and produced_information:
                sig = (call.name, path, model_text)
                if sig not in seen_info:
                    seen_info.add(sig)
                    made_progress = True

        for tool_index, call in enumerate(calls):
            path = _call_arg(call, "path", ".")
            try:
                if call.name != "shell":
                    emit(RunEvent.tool_started(
                        turn,
                        call,
                        render_tool_activity(call),
                        index=tool_index,
                    ))
                if call.name == "edit":
                    if _edit_has_content(call):
                        canonical = _canonical_project_path(project, path)
                        if safe_join(project, canonical).is_file():
                            outcome = ToolOutcome.error(
                                "content is only allowed when creating a new file; "
                                f"use replacements for existing file: {canonical}"
                            )
                        else:
                            if change_tracker is not None:
                                change_tracker.capture_before(path)
                            outcome = tool_fns.write_file(
                                project,
                                path,
                                _call_arg(call, "content"),
                            )
                    else:
                        guard = _read_before_edit_outcome(project, path, known_file_paths)
                        if guard is not None:
                            outcome = guard
                        else:
                            if change_tracker is not None:
                                change_tracker.capture_before(path)
                            outcome = tool_fns.edit_file(
                                project,
                                path,
                                _edit_blocks_from_call(call),
                            )
                    if outcome.ok and outcome.changed:
                        if change_tracker is not None:
                            change_tracker.capture_after(path)
                        made_progress = True
                        wrote_files = True
                        checks_passed = False
                        canonical = _canonical_project_path(project, path)
                        changed_files.add(canonical)
                        verification_paths.add(canonical)
                        known_file_paths.add(canonical)
                        edit_epoch += 1
                elif call.name == "read":
                    read_options = {
                        name: call.args[name]
                        for name in ("offset", "limit")
                        if name in call.args
                    }
                    outcome = tool_fns.read_file(project, path, **read_options)
                elif call.name == "ls":
                    outcome = tool_fns.list_directory(project, path)
                elif call.name == "search":
                    outcome = tool_fns.search_files(project, path, _call_arg(call, "query"))
                elif call.name == "references":
                    outcome = tool_fns.find_references(
                        project,
                        path,
                        _call_arg(call, "symbol"),
                    )
                elif call.name == "run":
                    command = _call_arg(call, "command")
                    outcome = tool_fns.execute_run_command(
                        project,
                        path,
                        command,
                        tool_id=f"{turn}:{tool_index}",
                    )
                    checks_ran = True
                    checks_passed = outcome.ok
                    if outcome.ok:
                        successful_verifications.append((command, path, edit_epoch))
                elif call.name == "shell":
                    command = _call_arg(call, "command").strip()
                    if on_shell_request:
                        on_shell_request(path, command)
                    emit(RunEvent.status(
                        f"[agent] shell approval requested: {command}"
                    ))
                    return finish("shell command requires approval", "approval", turn)
                else:
                    outcome = ToolOutcome.error(
                        f"malformed tool call {call.name} (path={path})"
                    )
            except cancellation.TaskCancelled:
                raise
            except Exception as exc:
                outcome = ToolOutcome.error(str(exc))
            record_tool_outcome(call, outcome, tool_index)
        if conversation is not None:
            conversation.update_snapshot(snapshot(control.body if control else ""))

        if control is None:
            if results:
                emit(RunEvent.status(
                    "[agent] reply had actions but no control element — assuming done."
                ))
                return finish(f"applied {len(results)} action(s) (no control element)", "protocol", turn)
            stagnant_count += 1
            if stagnant_count >= stagnant_turns:
                msg = f"stopped after {stagnant_turns} turns without valid tool progress"
                emit(RunEvent.status(f"[agent] {msg}."))
                return finish(msg, "no_progress", turn)
            emit(RunEvent.status(
                "[agent] reply contained no valid JSON tool call; nudging the model."
            ))
            repair = _protocol_repair_prompt(
                codec,
                ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error="no JSON tool call found",
                    protocol_error_kind=PROTOCOL_NO_JSON,
                ),
            )
            reply = send_prompt(
                repair,
                restart_request=repair,
                include_ghost_directive=False,
            )
            report_reply(turn + 1, reply, "(after nudge)")
            continue

        if control.kind == "done":
            # Safety net: if the model said `done` but also asked for info,
            # treat it as `continue` — it almost certainly needs the result.
            needs_followup = any(call.name in INFORMATION_TOOL_NAMES for call in calls)
            if needs_followup:
                emit(RunEvent.status(
                    "[agent] `done` came with info action — treating as continue."
                ))
            elif verification_required and wrote_files and not checks_passed:
                emit(RunEvent.status(
                    "[agent] verification was requested; asking model to run tests before done."
                ))
                if turn >= max_turns:
                    emit(RunEvent.status(
                        f"[agent] hit max_turns={max_turns}, stopping."
                    ))
                    return finish("verification required before done", "max_turns", turn)
                reminder = _verification_reminder(user_task)
                reply = send_prompt(reminder, restart_request=reminder)
                report_reply(turn + 1, reply, "(verification reminder)")
                continue
            else:
                candidate = selected_verification_candidate()
                trusted_green = verification_is_fresh(candidate)
                if candidate is not None:
                    checks_passed = trusted_green
                if (
                    not verification_required
                    and candidate is not None
                    and not trusted_green
                    and default_verification_reminded_epoch != edit_epoch
                ):
                    default_verification_reminded_epoch = edit_epoch
                    emit(RunEvent.status(
                        "[agent] code changed; asking model to handle the trusted check."
                    ))
                    if turn >= max_turns:
                        return finish("verification did not pass", "max_turns", turn)
                    reminder = _default_verification_reminder(candidate)
                    reply = send_prompt(reminder, restart_request=reminder)
                    report_reply(turn + 1, reply, "(default verification reminder)")
                    continue
                emit(RunEvent.status(f"[agent] DONE: {control.body}"))
                return finish(control.body, "done", turn)

        if made_progress:
            stagnant_count = 0
        else:
            stagnant_count += 1
            if stagnant_count >= stagnant_turns:
                msg = control.body or f"stopped after {stagnant_turns} turns without file writes or new tool information"
                emit(RunEvent.status(
                    f"[agent] no progress for {stagnant_turns} turns, stopping."
                ))
                return finish(msg, "no_progress", turn)

        if turn >= max_turns:
            emit(RunEvent.status(f"[agent] hit max_turns={max_turns}, stopping."))
            return finish(control.body or f"hit max_turns={max_turns}", "max_turns", turn)

        next_prompt = append_coding_context(codec.format_results(results))
        reply = send_prompt(
            next_prompt,
            restart_request=(
                "Continue the unfinished task using the latest local tool results below.\n\n"
                f"{next_prompt}"
            ),
        )
        report_reply(turn + 1, reply)

    return finish("(max turns reached)", "max_turns", max_turns)
