"""Tool-using agent on top of the DeepSeek web chat.

Why this protocol looks the way it does
---------------------------------------
DeepSeek's web UI renders ``` fenced blocks but only keeps the FIRST WORD of
the fence language line.  So if the model writes ``` ```write path=snake.py
``` `` DeepSeek's DOM only remembers "write" — the `path=snake.py` is gone.
The code body itself, however, is preserved byte-for-byte inside <pre>.

So Codey's protocol puts everything actionable INSIDE the code body as a
single magic marker comment on the first line.  Each fenced block is exactly
one action.  The language of the fence is ignored.

Protocol (we instruct the model up front):

    # === codey: write path=relative/path.ext ===
    <full file contents — verbatim>

    # === codey: edit path=relative/path.ext ===
    <<<<<<< SEARCH
    old exact text
    =======
    new exact text
    >>>>>>> REPLACE

    # === codey: read path=relative/path.ext ===

    # === codey: ls path=. ===

    # === codey: search path=. ===
    login handler

    # === codey: run path=. ===
    python -m unittest

    # === codey: shell path=. ===
    git status --short

    # === codey: done ===
    <one-line summary>

    # === codey: continue ===
    <short reason you need another turn>

Each action goes in its own fenced ``` block.  The fence language can be
anything (python, text, js, …); only the marker matters.
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

MARKER_RE = re.compile(
    r"^[#/\-\s]*===\s*codey\s*:\s*(\w+)(?:\s+path\s*=\s*([^\s=]+))?\s*===\s*(.*)$",
)

SYSTEM_PROMPT = """\
You are Codey, a careful local coding agent.  You can read, list, search,
edit and write files in the user's project.  You can run a small allowlist of
test/build commands, but you CANNOT run arbitrary shell commands.

OUTPUT PROTOCOL — every reply MUST follow this exactly.

Each action is one fenced ``` code block.  The FIRST LINE of every block is
a magic marker of the form

    # === codey: <action>[ path=<relative-path>] ===

The remaining lines of the block are the action's payload.  The fence
language (after the opening ```) is ignored — use whatever makes the code
look nice (python, text, js, etc.).

You may emit ZERO OR MORE action blocks (write/edit/read/ls/search/run/shell) then
EXACTLY ONE control block (done OR continue) at the end.

Actions:

  ```python
  # === codey: write path=relative/path.ext ===
  <full file contents go here, byte-perfect>
  ```

  ```text
  # === codey: edit path=relative/path.ext ===
  <<<<<<< SEARCH
  old exact text copied from `read`
  =======
  new replacement text
  >>>>>>> REPLACE
  ```

  ```text
  # === codey: read path=relative/path.ext ===
  ```

  ```text
  # === codey: ls path=. ===
  ```

  ```text
  # === codey: search path=. ===
  login handler
  ```

  ```text
  # === codey: run path=. ===
  python -m unittest
  ```

  ```text
  # === codey: shell path=. ===
  git status --short
  ```

Control (exactly one, last):

  ```text
  # === codey: done ===
  <one-line summary of what you accomplished>
  ```

  ```text
  # === codey: continue ===
  <short reason you need another turn>
  ```

Rules:
  - Paths are ALWAYS relative to the project root.  No absolute paths, no `..`.
  - The marker line MUST be exactly on line 1 of the code block.
  - Use `search` before `read` when you do not know which file contains the
    relevant code.
  - Prefer `edit` over `write` for small changes to existing files.  Use
    `write` for new files or when replacing a whole file is genuinely clearer.
  - Every `edit` SEARCH section must be copied exactly from content you read.
  - Use `run` only for tests/builds/checks such as `python -m unittest`,
    `python -m pytest`, `npm test`, `npm run build`, `go test ./...`,
    `cargo test`, or similar allowed verification commands.
  - Use `shell` only when a necessary command is not allowed by `run`.
    `shell` pauses the task and asks the user to approve the exact command.
    It is never executed automatically.
  - Never invent file contents you have not been shown.  Use `read` first.
  - Keep replies focused.  Plain commentary outside fenced blocks is fine
    for short explanations but the agent only acts on the marker blocks.

CRITICAL — when to use `done` vs `continue`:
  - `done` ends the entire task.  Only use it when the user's request is
    FULLY satisfied (all needed files written / verified).
  - `continue` means "I need another turn".  Use it whenever your reply
    contains a `read`, `ls`, `search`, `run` or `shell` action — the results
    arrive in the next turn and you will likely need to act on them.
  - A typical fix-a-bug flow takes TWO turns:
      turn 1:  read + continue            (asks to see the file)
      turn 2:  write + done               (writes the fixed file)
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


BLOCK_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```", re.MULTILINE | re.DOTALL)


def parse_reply(text: str) -> tuple[list[Action], Control | None]:
    actions: list[Action] = []
    control: Control | None = None
    for m in BLOCK_RE.finditer(text):
        body = m.group(1)
        lines = body.split("\n", 1)
        head = lines[0] if lines else ""
        rest = lines[1] if len(lines) > 1 else ""
        mm = MARKER_RE.match(head)
        if not mm:
            continue
        kind = mm.group(1).lower()
        path = mm.group(2)
        inline_body = mm.group(3).strip()
        if inline_body:
            rest = inline_body + (("\n" + rest) if rest else "")
        if kind in ("write", "edit", "read", "ls", "search", "run", "shell"):
            actions.append(Action(kind=kind, path=path, body=rest))
        elif kind in ("done", "continue"):
            control = Control(kind=kind, body=rest.strip())
    return actions, control


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
                on_event("[agent] reply had actions but no control block — assuming done.")
                return RunResult(f"applied {len(results)} action(s) (no done marker)", "protocol", turn)
            stagnant_count += 1
            if stagnant_count >= stagnant_turns:
                msg = f"stopped after {stagnant_turns} turns without valid tool progress"
                on_event(f"[agent] {msg}.")
                return RunResult(msg, "no_progress", turn)
            on_event("[agent] reply contained no valid marker blocks; nudging the model.")
            reply = chat(
                page,
                "Your previous reply did not contain any valid `# === codey: ... ===`"
                " marker blocks.  Please re-emit your work using the exact protocol."
                "  Remember: marker must be on line 1 inside ``` … ``` and the path"
                " goes there, NOT in the fence language.",
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
            + "\n\nContinue.  Remember: every block must start with"
              " `# === codey: <action>[ path=...] ===` on line 1, and end with a"
              " `done` or `continue` block."
        )
        reply = chat(page, next_prompt)
        on_event(f"\n--- turn {turn+1} reply ---\n{reply}\n")

    return RunResult("(max turns reached)", "max_turns", max_turns)
