from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from codey.protocols.base import Action, Control


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

CRITICAL - when to use `done` vs `continue`:
  - `done` ends the entire task. Only use it when the user's request is
    FULLY satisfied (all needed files written / verified).
  - `continue` means "I need another turn". Use it whenever your reply
    contains a `read`, `ls`, `search`, `run` or `shell` action - the results
    arrive in the next turn and you will likely need to act on them.
  - A typical fix-a-bug flow takes TWO turns:
      turn 1: read + continue             (asks to see the file)
      turn 2: edit/write + done           (writes the fixed file)
"""


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


class XmlToolCodec:
    name = "xml"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def parse(self, text: str) -> tuple[list[Action], Control | None]:
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
                    action = self._tool_action(child)
                    if action is not None:
                        actions.append(action)
                elif tag == "control":
                    kind = (child.attrib.get("type") or "").strip().lower()
                    if kind in ("continue", "done"):
                        control = Control(kind=kind, body="".join(child.itertext()).strip())
                elif tag in ("continue", "done"):
                    control = Control(kind=tag, body="".join(child.itertext()).strip())

        return actions, control

    def format_results(self, results: list[tuple[Action, str]]) -> str:
        chunks = []
        for action, output in results:
            head = f"[{action.kind} {action.path or ''}]".rstrip()
            chunks.append(f"{head}\n{output}")
        formatted = "\n\n".join(chunks) if chunks else "(no actions executed)"
        return (
            "Tool results from your previous actions:\n\n"
            f"{formatted}"
            "\n\nContinue. Remember: reply with exactly one well-formed"
            " <codey>...</codey> block, do not wrap it in markdown fences,"
            " and end with one <control type=\"done\">...</control> or"
            " <control type=\"continue\">...</control> element."
        )

    def repair_prompt(self) -> str:
        return (
            "Your previous reply did not contain a valid well-formed <codey>...</codey>"
            " block. Please re-emit your work using exactly one <codey> block."
            " Do not wrap it in markdown fences. End with exactly one"
            " <control type=\"continue\">...</control> or"
            " <control type=\"done\">...</control> element."
        )

    def _tool_action(self, element: ET.Element) -> Action | None:
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
