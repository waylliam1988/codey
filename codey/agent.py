"""Provider-agnostic tool-using agent runtime.

Codey asks a chat provider for JSON tool calls and converts those calls into
local ToolCall objects. The runtime only depends on ChatProvider and
ProtocolCodec interfaces; browser automation and provider-specific selectors
stay outside this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codey import cancellation
from codey.events import RunEvent, print_run_event
from codey.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_continuation_prompt,
)
from codey.models import ToolCall, ToolPlan, ToolResult
from codey.providers import ChatProvider
from codey.protocols import JsonToolCodec, ProtocolCodec
from codey.tool_runtime import (
    EditBlock,
    ToolOutcome,
    edit_file,
    list_directory,
    read_file,
    run_command,
    safe_join,
    search_files,
    write_file,
)

DEFAULT_MAX_TURNS = 50
DEFAULT_STAGNANT_TURNS = 4
SUPPORTED_TOOL_NAMES = frozenset({
    "edit",
    "ls",
    "read",
    "run",
    "search",
    "shell",
})
PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_INSTRUCTION_CHARS = 12000
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
        return "(no AGENTS.md or CLAUDE.md found)"
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
    codec: ProtocolCodec = DEFAULT_CODEC,
    max_turns: int = DEFAULT_MAX_TURNS,
    stagnant_turns: int = DEFAULT_STAGNANT_TURNS,
    on_event=print_run_event,
    on_shell_request=None,
    stop_flag=None,
    fresh_chat: bool = True,
    change_tracker: ChangeTracker | None = None,
    conversation: ConversationContext | None = None,
    provider_id: str = "",
    handoff: str = "",
    project_facts: str = "",
    project_map: str = "",
) -> RunResult:
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    max_turns = max(1, int(max_turns or DEFAULT_MAX_TURNS))
    stagnant_turns = max(1, int(stagnant_turns or DEFAULT_STAGNANT_TURNS))
    seen_info: set[tuple[str, str, str]] = set()
    stagnant_count = 0
    wrote_files = False
    checks_passed = False
    checks_ran = False
    changed_files = set(conversation.snapshot.changed_files if conversation else ())
    verification_required = _task_requests_verification(user_task)
    project_text = str(project)
    active_provider_id = provider_id or getattr(provider, "name", "")

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
            emit(RunEvent.status(
                f"[agent] could not open new chat: {exc}; reusing current tab"
            ))
            return False
        return True

    project_instructions = load_project_instructions(project)
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        emit(RunEvent.info("loaded project instructions", names=names))

    def project_intro(request: str, factual_handoff: str = "") -> str:
        current = (
            render_continuation_prompt(factual_handoff, request)
            if factual_handoff
            else request
        )
        facts = (
            f"Verified project facts from successful local runs:\n{project_facts}\n\n"
            if project_facts
            else ""
        )
        map_block = (
            f"{project_map}\n\n"
            if project_map
            else ""
        )
        return (
            f"{codec.system_prompt()}\n\n"
            f"Project root: {project}\n"
            f"Project instructions:\n{format_project_instructions(project_instructions)}\n\n"
            f"{facts}"
            f"{map_block}"
            f"Initial listing:\n{list_directory(project, '.').output}\n\n"
            f"User task:\n{current}"
        )

    def send_prompt(prompt: str, *, restart_request: str | None = None) -> str:
        opened_fresh_chat = False
        if conversation is not None and conversation.needs_rollover(prompt):
            factual_handoff = conversation.prepare_model_handoff(provider.send)
            if open_fresh_chat():
                prompt = project_intro(restart_request or prompt, factual_handoff)
                opened_fresh_chat = True
        reply_text = provider.send(prompt)
        if conversation is not None:
            if opened_fresh_chat:
                conversation.begin_window(active_provider_id, "project", project_text)
            conversation.record_exchange(prompt, reply_text, snapshot())
        return reply_text

    if fresh_chat:
        opened_fresh_chat = open_fresh_chat()
        intro = project_intro(user_task, handoff)
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
            repair = (
                f"Protocol error: {plan.protocol_error}\n\n"
                f"{codec.repair_prompt()}"
            )
            reply = send_prompt(repair, restart_request=repair)
            report_reply(turn + 1, reply, "(after protocol correction)")
            continue
        calls = plan.calls
        control = plan.control

        results: list[ToolResult] = []
        made_progress = False
        for call in calls:
            path = _call_arg(call, "path", ".")
            try:
                if call.name == "edit":
                    if _edit_has_content(call):
                        if change_tracker is not None:
                            change_tracker.capture_before(path)
                        outcome = write_file(project, path, _call_arg(call, "content"))
                    else:
                        if change_tracker is not None:
                            change_tracker.capture_before(path)
                        outcome = edit_file(project, path, _edit_blocks_from_call(call))
                    if outcome.ok and outcome.changed:
                        if change_tracker is not None:
                            change_tracker.capture_after(path)
                        made_progress = True
                        wrote_files = True
                        checks_passed = False
                        changed_files.add(path)
                elif call.name == "read":
                    read_options = {
                        name: call.args[name]
                        for name in ("offset", "limit")
                        if name in call.args
                    }
                    outcome = read_file(project, path, **read_options)
                elif call.name == "ls":
                    outcome = list_directory(project, path)
                elif call.name == "search":
                    outcome = search_files(project, path, _call_arg(call, "query"))
                elif call.name == "run":
                    outcome = run_command(project, path, _call_arg(call, "command"))
                    checks_ran = True
                    checks_passed = outcome.ok
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
            out = outcome.output
            emit(RunEvent.tool_finished(turn, call, outcome))
            results.append(ToolResult(call=call, output=out, truncated=outcome.truncated))
            produced_information = outcome.ok or outcome.exit_code is not None
            if call.name in ("read", "ls", "search", "run", "shell") and produced_information:
                sig = (call.name, path, out)
                if sig not in seen_info:
                    seen_info.add(sig)
                    made_progress = True
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
            repair = codec.repair_prompt()
            reply = send_prompt(repair, restart_request=repair)
            report_reply(turn + 1, reply, "(after nudge)")
            continue

        if control.kind == "done":
            # Safety net: if the model said `done` but also asked for info,
            # treat it as `continue` — it almost certainly needs the result.
            needs_followup = any(call.name in ("read", "ls", "search", "run", "shell") for call in calls)
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

        next_prompt = codec.format_results(results)
        reply = send_prompt(
            next_prompt,
            restart_request=(
                "Continue the unfinished task using the latest local tool results below.\n\n"
                f"{next_prompt}"
            ),
        )
        report_reply(turn + 1, reply)

    return finish("(max turns reached)", "max_turns", max_turns)
