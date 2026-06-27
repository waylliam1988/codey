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

    # === codey: read path=relative/path.ext ===

    # === codey: ls path=. ===

    # === codey: done ===
    <one-line summary>

    # === codey: continue ===
    <short reason you need another turn>

Each action goes in its own fenced ``` block.  The fence language can be
anything (python, text, js, …); only the marker matters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page

from codey.deepseek import chat, new_chat

DEFAULT_MAX_TURNS = 50
DEFAULT_STAGNANT_TURNS = 4
PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_PROJECT_INSTRUCTION_CHARS = 12000

MARKER_RE = re.compile(
    r"^[#/\-\s]*===\s*codey\s*:\s*(\w+)(?:\s+path\s*=\s*([^\s=]+))?\s*===\s*$",
    re.MULTILINE,
)

SYSTEM_PROMPT = """\
You are Codey, a careful local coding agent.  You can read, list and write
files in the user's project.  You CANNOT run shell commands.

OUTPUT PROTOCOL — every reply MUST follow this exactly.

Each action is one fenced ``` code block.  The FIRST LINE of every block is
a magic marker of the form

    # === codey: <action>[ path=<relative-path>] ===

The remaining lines of the block are the action's payload.  The fence
language (after the opening ```) is ignored — use whatever makes the code
look nice (python, text, js, etc.).

You may emit ZERO OR MORE action blocks (write/read/ls) then EXACTLY ONE
control block (done OR continue) at the end.

Actions:

  ```python
  # === codey: write path=relative/path.ext ===
  <full file contents go here, byte-perfect>
  ```

  ```text
  # === codey: read path=relative/path.ext ===
  ```

  ```text
  # === codey: ls path=. ===
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
  - Never invent file contents you have not been shown.  Use `read` first.
  - Keep replies focused.  Plain commentary outside fenced blocks is fine
    for short explanations but the agent only acts on the marker blocks.

CRITICAL — when to use `done` vs `continue`:
  - `done` ends the entire task.  Only use it when the user's request is
    FULLY satisfied (all needed files written / verified).
  - `continue` means "I need another turn".  Use it whenever your reply
    contains a `read` or `ls` action — the file contents arrive in the
    next turn and you will likely need to act on them.
  - A typical fix-a-bug flow takes TWO turns:
      turn 1:  read + continue            (asks to see the file)
      turn 2:  write + done               (writes the fixed file)
"""


# ---------------------------------------------------------------- parsing ---

@dataclass
class Action:
    kind: str          # "write" | "read" | "ls"
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
        if kind in ("write", "read", "ls"):
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
                elif a.kind == "read" and a.path:
                    out = tool_read(project, a.path)
                elif a.kind == "ls":
                    out = tool_ls(project, a.path or ".")
                else:
                    out = f"ERROR: malformed action {a.kind} (path={a.path})"
            except Exception as exc:
                out = f"ERROR: {exc}"
            on_event(f"  · {a.kind} {a.path or ''} -> {out.splitlines()[0][:80]}")
            results.append((a, out))
            if a.kind in ("read", "ls") and not out.startswith("ERROR:"):
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
            # Safety net: if the model said `done` but also asked to read/ls,
            # treat it as `continue` — it almost certainly needs the result.
            needs_followup = any(a.kind in ("read", "ls") for a in actions)
            if needs_followup:
                on_event("[agent] `done` came with read/ls — treating as continue.")
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
