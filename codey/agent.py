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
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page

from codey.deepseek import chat, new_chat

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

SYSTEM_PROMPT = """\
You are Codey, a careful local coding agent. You can read, list, search,
edit and write files in the user's project. You can run a small allowlist of
test/build commands, but you CANNOT run arbitrary shell commands.

OUTPUT PROTOCOL - every reply MUST contain exactly one well-formed
<codey>...</codey> block. Do not wrap the <codey> block in markdown fences.
Plain commentary outside the block is allowed, but Codey only acts on the XML.

Inside <codey>, emit ZERO OR MORE tool calls, then EXACTLY ONE control element
at the end:

  <codey>
    <tool name="search">
      <path>.</path>
      <query>login handler</query>
    </tool>
    <control type="continue">Need search results</control>
  </codey>

Tools:

  <tool name="read">
    <path>relative/path.ext</path>
  </tool>

  <tool name="ls">
    <path>.</path>
  </tool>

  <tool name="search">
    <path>.</path>
    <query>login handler</query>
  </tool>

  <tool name="write">
    <path>relative/path.ext</path>
    <content><![CDATA[
full file contents go here, byte-perfect
]]></content>
  </tool>

  <tool name="edit">
    <path>relative/path.ext</path>
    <replace>
      <search><![CDATA[
old exact text copied from read
]]></search>
      <with><![CDATA[
new replacement text
]]></with>
    </replace>
  </tool>

  <tool name="run">
    <path>.</path>
    <command>python -m unittest</command>
  </tool>

  <tool name="shell">
    <path>.</path>
    <command>git status --short</command>
  </tool>

Control (exactly one, last):

  <control type="done">One-line summary of what you accomplished</control>
  <control type="continue">Short reason you need another turn</control>

Rules:
  - XML must be well-formed. For code, diffs, and any text containing <, >, or
    &, use CDATA exactly as shown above.
  - Paths are ALWAYS relative to the project root. No absolute paths, no `..`.
  - Use `search` before `read` when you do not know which file contains the
    relevant code.
  - Prefer `edit` over `write` for small changes to existing files. Use
    `write` for new files or when replacing a whole file is genuinely clearer.
  - Every `edit` SEARCH section must be copied exactly from content you read.
  - Use `run` only for tests/builds/checks such as `python -m unittest`,
    `python -m pytest`, `npm test`, `npm run build`, `go test ./...`,
    `cargo test`, or similar allowed verification commands.
  - Use `shell` only when a necessary command is not allowed by `run`.
    `shell` pauses the task and asks the user to approve the exact command.
    It is never executed automatically.
  - Never invent file contents you have not been shown.  Use `read` first.
  - Keep replies focused. The <codey> block is the only actionable part.

CRITICAL — when to use `done` vs `continue`:
  - `done` ends the entire task. Only use it when the user's request is
    FULLY satisfied (all needed files written / verified).
  - `continue` means "I need another turn". Use it whenever your reply
    contains a `read`, `ls`, `search`, `run` or `shell` action — the results
    arrive in the next turn and you will likely need to act on them.
  - A typical fix-a-bug flow takes TWO turns:
      turn 1: read + continue             (asks to see the file)
      turn 2: edit/write + done           (writes the fixed file)
"""


# ---------------------------------------------------------------- parsing ---

@dataclass
class Action:
    kind: str          # "write" | "edit" | "read" | "ls" | "search" | "run" | "shell"
    path: str | None
    body: str


@dataclass
class Control:
    kind: str          # "continue" | "done"
    body: str


@dataclass(frozen=True)
class ProjectInstruction:
    name: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class EditBlock:
    search: str
    replace: str


CODEY_XML_RE = re.compile(r"<codey\b[^>]*>.*?</codey>", re.IGNORECASE | re.DOTALL)


def _xml_tag_name(element: ET.Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.lower()


def _xml_child_text(element: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element:
        if _xml_tag_name(child) in wanted:
            return "".join(child.itertext())
    return ""


def _xml_payload(text: str) -> str:
    """Remove the wrapper newline models usually add inside CDATA blocks."""
    if text.startswith("\n"):
        text = text[1:]
    if text.endswith("\n"):
        text = text[:-1]
    return text


def _xml_tool_action(element: ET.Element) -> Action | None:
    kind = (element.attrib.get("name") or element.attrib.get("tool") or "").strip().lower()
    if kind not in ("write", "edit", "read", "ls", "search", "run", "shell"):
        return None

    path = (_xml_child_text(element, "path", "cwd") or element.attrib.get("path") or "").strip()
    body = ""

    if kind == "write":
        body = _xml_payload(_xml_child_text(element, "content", "body", "text"))
    elif kind == "edit":
        blocks: list[str] = []
        for child in element:
            if _xml_tag_name(child) != "replace":
                continue
            search = _xml_payload(_xml_child_text(child, "search"))
            replacement = _xml_payload(_xml_child_text(child, "with", "replacement", "replace"))
            blocks.append(f"<<<<<<< SEARCH\n{search}\n=======\n{replacement}\n>>>>>>> REPLACE")
        body = "\n".join(blocks)
    elif kind == "search":
        body = (_xml_child_text(element, "query", "body", "text") or "").strip()
    elif kind in ("run", "shell"):
        body = (_xml_child_text(element, "command", "cmd", "body", "text") or "").strip()

    return Action(kind=kind, path=path or None, body=body)


def _parse_xml_reply(text: str) -> tuple[list[Action], Control | None]:
    actions: list[Action] = []
    control: Control | None = None

    for match in CODEY_XML_RE.finditer(text):
        try:
            root = ET.fromstring(match.group(0))
        except ET.ParseError:
            continue
        if _xml_tag_name(root) != "codey":
            continue

        for child in root:
            tag = _xml_tag_name(child)
            if tag == "tool":
                action = _xml_tool_action(child)
                if action is not None:
                    actions.append(action)
            elif tag == "control":
                kind = (child.attrib.get("type") or "").strip().lower()
                if kind in ("continue", "done"):
                    control = Control(kind=kind, body="".join(child.itertext()).strip())
            elif tag in ("continue", "done"):
                control = Control(kind=tag, body="".join(child.itertext()).strip())

    return actions, control


def parse_reply(text: str) -> tuple[list[Action], Control | None]:
    return _parse_xml_reply(text)


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


# ----------------------------------------------------------------- loop ---

@dataclass
class StepResult:
    actions: list[Action]
    control: Control | None
    tool_outputs: list[str] = field(default_factory=list)
    raw_reply: str = ""


@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"  # done | stopped | max_turns | no_progress | protocol
    turns: int = 0


def _format_tool_results(results: list[tuple[Action, str]]) -> str:
    chunks = []
    for action, output in results:
        head = f"[{action.kind} {action.path or ''}]".rstrip()
        chunks.append(f"{head}\n{output}")
    return "\n\n".join(chunks)


def run(
    page: Page,
    project: Path,
    user_task: str,
    *,
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
        f"{SYSTEM_PROMPT}\n\n"
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
        actions, control = parse_reply(reply)

        results: list[tuple[Action, str]] = []
        made_progress = False
        for a in actions:
            try:
                if a.kind == "write" and a.path:
                    out = tool_write(project, a.path, a.body)
                    if not out.startswith("ERROR:"):
                        made_progress = True
                elif a.kind == "edit" and a.path:
                    out = tool_edit(project, a.path, a.body)
                    if not out.startswith("ERROR:"):
                        made_progress = True
                elif a.kind == "read" and a.path:
                    out = tool_read(project, a.path)
                elif a.kind == "ls":
                    out = tool_ls(project, a.path or ".")
                elif a.kind == "search":
                    out = tool_search(project, a.path or ".", a.body)
                elif a.kind == "run":
                    out = tool_run(project, a.path or ".", a.body)
                elif a.kind == "shell":
                    command = a.body.strip()
                    if on_shell_request:
                        on_shell_request(a.path or ".", command)
                    on_event(f"[agent] shell approval requested: {command}")
                    return RunResult("shell command requires approval", "approval", turn)
                else:
                    out = f"ERROR: malformed action {a.kind} (path={a.path})"
            except Exception as exc:
                out = f"ERROR: {exc}"
            on_event(f"  · {a.kind} {a.path or ''} -> {out.splitlines()[0][:80]}")
            results.append((a, out))
            if a.kind in ("read", "ls", "search", "run", "shell") and not out.startswith("ERROR:"):
                sig = (a.kind, a.path or ".", out)
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
                "Your previous reply did not contain a valid well-formed <codey>...</codey>"
                " block. Please re-emit your work using exactly one <codey> block."
                " Do not wrap it in markdown fences. End with exactly one"
                " <control type=\"continue\">...</control> or"
                " <control type=\"done\">...</control> element.",
            )
            on_event(f"\n--- turn {turn+1} reply (after nudge) ---\n{reply}\n")
            continue

        if control.kind == "done":
            # Safety net: if the model said `done` but also asked for info,
            # treat it as `continue` — it almost certainly needs the result.
            needs_followup = any(a.kind in ("read", "ls", "search", "run", "shell") for a in actions)
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

        next_prompt = (
            "Tool results from your previous actions:\n\n"
            + (_format_tool_results(results) if results else "(no actions executed)")
            + "\n\nContinue. Remember: reply with exactly one well-formed"
              " <codey>...</codey> block, do not wrap it in markdown fences,"
              " and end with one <control type=\"done\">...</control> or"
              " <control type=\"continue\">...</control> element."
        )
        reply = chat(page, next_prompt)
        on_event(f"\n--- turn {turn+1} reply ---\n{reply}\n")

    return RunResult("(max turns reached)", "max_turns", max_turns)
