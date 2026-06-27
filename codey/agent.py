"""Tool-using agent on top of the DeepSeek web chat.

Codey asks the web model for XML-like tool tags and converts those tags into
local actions. XML is the protocol boundary because code/edit payloads can be
placed in CDATA without JSON escaping, while Codey's execution layer only sees
structured actions.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page

from codey.deepseek import chat, new_chat
from codey.models import Control, ToolCall, ToolPlan, ToolResult
from codey.protocols import ProtocolCodec, XmlToolCodec

DEFAULT_MAX_TURNS = 50
DEFAULT_STAGNANT_TURNS = 4
PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_INSTRUCTION_CHARS = 12000
SEARCH_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".turbo",
    "target",
}
SEARCH_MAX_RESULTS = 80
SEARCH_MAX_FILE_BYTES = 512 * 1024
RUN_TIMEOUT_SECONDS = 90
RUN_OUTPUT_LIMIT = 24_000
RUN_FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "$(", "`"}
RUN_ALLOWED_NPM_SCRIPTS = {"test", "build", "lint", "check", "typecheck"}
EDIT_BLOCK_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)
DEFAULT_CODEC = XmlToolCodec()


@dataclass(frozen=True)
class ProjectInstruction:
    name: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class EditBlock:
    search: str
    replace: str


def parse_reply(text: str, codec: ProtocolCodec = DEFAULT_CODEC) -> ToolPlan:
    return codec.parse(text)


# ----------------------------------------------------------------- tools ---

def _safe_join(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    root_resolved = root.resolve()
    if root_resolved not in p.parents and p != root_resolved:
        raise ValueError(f"path escapes project root: {rel}")
    return p


def tool_write(root: Path, rel: str, content: str) -> str:
    p = _safe_join(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {rel} ({len(content)} chars)"


def parse_edit_body(body: str) -> list[EditBlock]:
    matches = list(EDIT_BLOCK_RE.finditer(body.strip()))
    if not matches:
        raise ValueError("edit body must contain SEARCH/REPLACE block")
    leftover = EDIT_BLOCK_RE.sub("", body.strip()).strip()
    if leftover:
        raise ValueError("edit body contains text outside SEARCH/REPLACE blocks")
    blocks: list[EditBlock] = []
    for m in matches:
        search = m.group(1)
        replace = m.group(2)
        if not search:
            raise ValueError("SEARCH section cannot be empty")
        blocks.append(EditBlock(search=search, replace=replace))
    return blocks


def _replace_unique(content: str, search: str, replace: str) -> tuple[str, bool]:
    count = content.count(search)
    if count == 1:
        return content.replace(search, replace, 1), True
    if count == 0 and "\r\n" in content and "\r\n" not in search:
        crlf_search = search.replace("\n", "\r\n")
        crlf_replace = replace.replace("\n", "\r\n")
        crlf_count = content.count(crlf_search)
        if crlf_count == 1:
            return content.replace(crlf_search, crlf_replace, 1), True
    return content, False


def tool_edit(root: Path, rel: str, body: str) -> str:
    p = _safe_join(root, rel)
    if not p.is_file():
        return f"ERROR: not a file: {rel}"
    try:
        blocks = parse_edit_body(body)
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: not utf-8 text: {rel}"
    except ValueError as exc:
        return f"ERROR: {exc}"

    updated = content
    for block in blocks:
        exact_count = updated.count(block.search)
        crlf_count = 0
        if exact_count == 0 and "\r\n" in updated and "\r\n" not in block.search:
            crlf_count = updated.count(block.search.replace("\n", "\r\n"))
        total = exact_count or crlf_count
        if total == 0:
            return f"ERROR: SEARCH text not found in {rel}"
        if total > 1:
            return f"ERROR: SEARCH text matched {total} times in {rel}; make it unique"
        updated, ok = _replace_unique(updated, block.search, block.replace)
        if not ok:
            return f"ERROR: SEARCH text not found in {rel}"

    if updated == content:
        return f"edited {rel} (no changes)"
    p.write_text(updated, encoding="utf-8")
    return f"edited {rel} ({len(blocks)} replacement{'s' if len(blocks) != 1 else ''})"


def tool_read(root: Path, rel: str) -> str:
    p = _safe_join(root, rel)
    if not p.is_file():
        return f"ERROR: not a file: {rel}"
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: not utf-8 text: {rel}"


def tool_ls(root: Path, rel: str) -> str:
    p = _safe_join(root, rel)
    if not p.is_dir():
        return f"ERROR: not a directory: {rel}"
    lines: list[str] = []
    for entry in sorted(p.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            for sub in sorted(entry.iterdir())[:50]:
                if sub.name.startswith("."):
                    continue
                tag = "/" if sub.is_dir() else ""
                lines.append(f"  {sub.name}{tag}")
        else:
            lines.append(entry.name)
    return "\n".join(lines) if lines else "(empty)"


def _searchable_files(start: Path) -> list[Path]:
    if start.is_file():
        return [start]
    files: list[Path] = []
    stack = [start]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SEARCH_EXCLUDED_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    return files


def tool_search(
    root: Path,
    rel: str,
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
) -> str:
    query = query.strip()
    if not query:
        return "ERROR: search query required"
    start = _safe_join(root, rel or ".")
    if not start.exists():
        return f"ERROR: path not found: {rel}"
    needle = query.lower()
    matches: list[str] = []
    truncated = False
    root_resolved = root.resolve()
    for path in _searchable_files(start):
        try:
            if path.stat().st_size > SEARCH_MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle not in line.lower():
                continue
            rel_path = path.relative_to(root_resolved).as_posix()
            clean = line.strip()
            if len(clean) > 240:
                clean = clean[:237] + "..."
            matches.append(f"{rel_path}:{line_no}: {clean}")
            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break
    if not matches:
        return "(no matches)"
    if truncated:
        matches.append(f"... truncated after {max_results} matches")
    return "\n".join(matches)


def _command_has_forbidden_tokens(argv: list[str]) -> bool:
    return any(token in arg for arg in argv for token in RUN_FORBIDDEN_TOKENS)


def _is_allowed_run_command(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = Path(argv[0]).name.lower()
    if exe in {"python", "python.exe", "py", "py.exe"}:
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] in {"unittest", "pytest", "py_compile"}:
            return True
        if len(argv) >= 2 and argv[1].endswith(".py"):
            return True
        return False
    if exe in {"pytest", "pytest.exe"}:
        return True
    if exe in {"npm", "npm.cmd", "npm.exe"}:
        return len(argv) >= 2 and (
            argv[1] in RUN_ALLOWED_NPM_SCRIPTS
            or (len(argv) >= 3 and argv[1] == "run" and argv[2] in RUN_ALLOWED_NPM_SCRIPTS)
        )
    if exe in {"pnpm", "pnpm.cmd", "pnpm.exe", "yarn", "yarn.cmd", "yarn.exe"}:
        return len(argv) >= 2 and (
            argv[1] in RUN_ALLOWED_NPM_SCRIPTS
            or (len(argv) >= 3 and argv[1] == "run" and argv[2] in RUN_ALLOWED_NPM_SCRIPTS)
        )
    if exe in {"go", "go.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build", "vet"}
    if exe in {"cargo", "cargo.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build", "check"}
    if exe in {"dotnet", "dotnet.exe"}:
        return len(argv) >= 2 and argv[1] in {"test", "build"}
    return False


def tool_run(root: Path, rel: str, command: str) -> str:
    command = command.strip()
    if not command:
        return "ERROR: command required"
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"ERROR: invalid command: {exc}"
    if _command_has_forbidden_tokens(argv) or not _is_allowed_run_command(argv):
        return f"ERROR: command not allowed: {command}"
    cwd = _safe_join(root, rel or ".")
    if not cwd.is_dir():
        return f"ERROR: not a directory: {rel}"
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return f"ERROR: command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {RUN_TIMEOUT_SECONDS}s: {command}"

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    truncated = len(output) > RUN_OUTPUT_LIMIT
    if truncated:
        output = output[:RUN_OUTPUT_LIMIT].rstrip() + "\n\n... output truncated by Codey"
    return f"exit {proc.returncode}: {command}\n{output}"


def load_project_instructions(
    root: Path,
    *,
    max_chars: int = MAX_PROJECT_INSTRUCTION_CHARS,
) -> list[ProjectInstruction]:
    docs: list[ProjectInstruction] = []
    for name in PROJECT_INSTRUCTION_FILES:
        path = _safe_join(root, name)
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars].rstrip() + "\n\n[truncated by Codey]"
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


def _edit_body_from_call(call: ToolCall) -> str:
    replacements = call.args.get("replacements")
    if not isinstance(replacements, list):
        return ""
    blocks: list[str] = []
    for item in replacements:
        if not isinstance(item, dict):
            continue
        search = str(item.get("search") or "")
        replace = str(item.get("replace") or "")
        blocks.append(f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE")
    return "\n".join(blocks)


# ----------------------------------------------------------------- loop ---

@dataclass
class StepResult:
    calls: list[ToolCall]
    control: Control | None
    tool_outputs: list[str] = field(default_factory=list)
    raw_reply: str = ""


@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"  # done | stopped | max_turns | no_progress | protocol
    turns: int = 0


def run(
    page: Page,
    project: Path,
    user_task: str,
    *,
    codec: ProtocolCodec = DEFAULT_CODEC,
    max_turns: int = DEFAULT_MAX_TURNS,
    stagnant_turns: int = DEFAULT_STAGNANT_TURNS,
    on_event=print,
    on_shell_request=None,
    stop_flag=None,
    fresh_chat: bool = True,
) -> RunResult:
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    max_turns = max(1, int(max_turns or DEFAULT_MAX_TURNS))
    stagnant_turns = max(1, int(stagnant_turns or DEFAULT_STAGNANT_TURNS))
    seen_info: set[tuple[str, str, str]] = set()
    stagnant_count = 0

    if fresh_chat:
        on_event("[agent] opening a fresh DeepSeek conversation")
        try:
            new_chat(page)
        except Exception as exc:
            on_event(f"[agent] could not open new chat: {exc}; reusing current tab")

    project_instructions = load_project_instructions(project)
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        on_event(f"[agent] loaded project instructions: {names}")

    intro = (
        f"{codec.system_prompt()}\n\n"
        f"Project root: {project}\n"
        f"Project instructions:\n{format_project_instructions(project_instructions)}\n\n"
        f"Initial listing:\n{tool_ls(project, '.')}\n\n"
        f"User task:\n{user_task}"
    )
    reply = chat(page, intro)
    on_event(f"\n--- turn 1 reply ---\n{reply}\n")

    for turn in range(1, max_turns + 1):
        if stop_flag is not None and stop_flag.is_set():
            on_event("[agent] stopped by user.")
            return RunResult("stopped", "stopped", turn)
        plan = parse_reply(reply, codec)
        calls = plan.calls
        control = plan.control

        results: list[ToolResult] = []
        made_progress = False
        for call in calls:
            path = _call_arg(call, "path", ".")
            try:
                if call.name == "write":
                    out = tool_write(project, path, _call_arg(call, "content"))
                    if not out.startswith("ERROR:"):
                        made_progress = True
                elif call.name == "edit":
                    out = tool_edit(project, path, _edit_body_from_call(call))
                    if not out.startswith("ERROR:"):
                        made_progress = True
                elif call.name == "read":
                    out = tool_read(project, path)
                elif call.name == "ls":
                    out = tool_ls(project, path)
                elif call.name == "search":
                    out = tool_search(project, path, _call_arg(call, "query"))
                elif call.name == "run":
                    out = tool_run(project, path, _call_arg(call, "command"))
                elif call.name == "shell":
                    command = _call_arg(call, "command").strip()
                    if on_shell_request:
                        on_shell_request(path, command)
                    on_event(f"[agent] shell approval requested: {command}")
                    return RunResult("shell command requires approval", "approval", turn)
                else:
                    out = f"ERROR: malformed tool call {call.name} (path={path})"
            except Exception as exc:
                out = f"ERROR: {exc}"
            on_event(f"  · {call.name} {path if path != '.' else ''} -> {out.splitlines()[0][:80]}")
            results.append(ToolResult(call=call, output=out))
            if call.name in ("read", "ls", "search", "run", "shell") and not out.startswith("ERROR:"):
                sig = (call.name, path, out)
                if sig not in seen_info:
                    seen_info.add(sig)
                    made_progress = True

        if control is None:
            if results:
                on_event("[agent] reply had actions but no control element — assuming done.")
                return RunResult(f"applied {len(results)} action(s) (no control element)", "protocol", turn)
            stagnant_count += 1
            if stagnant_count >= stagnant_turns:
                msg = f"stopped after {stagnant_turns} turns without valid tool progress"
                on_event(f"[agent] {msg}.")
                return RunResult(msg, "no_progress", turn)
            on_event("[agent] reply contained no valid <codey> block; nudging the model.")
            reply = chat(
                page,
                codec.repair_prompt(),
            )
            on_event(f"\n--- turn {turn+1} reply (after nudge) ---\n{reply}\n")
            continue

        if control.kind == "done":
            # Safety net: if the model said `done` but also asked for info,
            # treat it as `continue` — it almost certainly needs the result.
            needs_followup = any(call.name in ("read", "ls", "search", "run", "shell") for call in calls)
            if needs_followup:
                on_event("[agent] `done` came with info action — treating as continue.")
            else:
                on_event(f"[agent] DONE: {control.body}")
                return RunResult(control.body, "done", turn)

        if made_progress:
            stagnant_count = 0
        else:
            stagnant_count += 1
            if stagnant_count >= stagnant_turns:
                msg = control.body or f"stopped after {stagnant_turns} turns without file writes or new tool information"
                on_event(f"[agent] no progress for {stagnant_turns} turns, stopping.")
                return RunResult(msg, "no_progress", turn)

        if turn >= max_turns:
            on_event(f"[agent] hit max_turns={max_turns}, stopping.")
            return RunResult(control.body or f"hit max_turns={max_turns}", "max_turns", turn)

        next_prompt = codec.format_results(results)
        reply = chat(page, next_prompt)
        on_event(f"\n--- turn {turn+1} reply ---\n{reply}\n")

    return RunResult("(max turns reached)", "max_turns", max_turns)
