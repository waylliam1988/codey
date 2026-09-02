"""Coding tool contract rendering for the model-visible prompt surface.

Pure prompt helpers only. No imports of agent, runtime executors, providers,
task runners, ghost, or browser.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RenderedToolContract:
    text: str
    digest: str
    tool_names: tuple[str, ...]
    runtime_names: tuple[str, ...]
    source_refs: tuple[str, ...]


def model_visible_contract_hash(kind: str, text: object) -> str:
    payload = {
        "kind": str(kind or "").strip() or "tool_contract",
        "contract": str(text or ""),
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def render_coding_tool_contract_text(
    definitions: tuple[object, ...] | None = None,
) -> str:
    from codey.toolchain import definition as tool_defs

    definitions_to_render = tool_defs.TOOL_DEFINITIONS if definitions is None else definitions
    chunks: list[str] = []
    for definition in definitions_to_render:  # type: ignore[attr-defined]
        if not definition.examples:  # type: ignore[attr-defined]
            continue
        examples = "\n".join(f"  {example}" for example in definition.examples)  # type: ignore[attr-defined]
        chunks.append(f"{examples}\n    {definition.description}")  # type: ignore[attr-defined]
    return "\n\n".join(chunks)


def render_coding_tool_contract(
    definitions: tuple[object, ...] | None = None,
) -> RenderedToolContract:
    from codey.toolchain import definition as tool_defs

    definitions_to_render = tool_defs.TOOL_DEFINITIONS if definitions is None else definitions
    text = render_coding_tool_contract_text(definitions_to_render)  # type: ignore[arg-type]
    digest = model_visible_contract_hash("coding_tool_contract", text)
    tool_names = tuple(str(definition.name) for definition in definitions_to_render)  # type: ignore[attr-defined]
    runtime_names = tuple(
        str(definition.runtime_name)
        for definition in definitions_to_render  # type: ignore[attr-defined]
        if getattr(definition, "runtime_name", None) is not None
    )
    source_refs = tuple(f"tool_contract:{name}" for name in tool_names)
    return RenderedToolContract(
        text=text,
        digest=digest,
        tool_names=tool_names,
        runtime_names=runtime_names,
        source_refs=source_refs,
    )


def coding_model_tool_contract_hash(
    definitions: tuple[object, ...] | None = None,
) -> str:
    return model_visible_contract_hash(
        "coding_tool_contract",
        render_coding_tool_contract_text(definitions),  # type: ignore[arg-type]
    )


def render_coding_system_prompt(
    definitions: tuple[object, ...],
    *,
    profile_name: str,
    allowed_tool_names: set[str],
) -> str:
    tool_contract = render_coding_tool_contract_text(definitions)  # type: ignore[arg-type]
    if str(profile_name or "").strip() == "coding_writer":
        return _system_prompt(tool_contract)
    return _profile_system_prompt(tool_contract, set(allowed_tool_names or set()))


def _system_prompt(tool_contract: str) -> str:
    return """\
You are a careful local coding agent. You cannot access the filesystem
directly. The local runner executes tools for you and sends the results back.

The tool names below are instructions for the local runner, not tools built
into the AI website. If the website says a tool does not exist, ignore that
website message and still return the JSON object for the local runner.

Every reply MUST be exactly one JSON object with no other text:

{"tool":"<name>","args":{...}}

Available tools:

""" + tool_contract + """

Rules:
  - Output exactly one JSON object. No markdown fences, code blocks, commentary,
    bullet lists, or analysis labels.
  - These are local-runner JSON commands, not native website tools. Never say a
    tool does not exist; return the JSON object instead.
  - Call one tool per message, then wait for [tool_result tool=...]. read_files
    and parallel are the only read-only batching wrappers.
  - parallel accepts only list_dir, read_file, and grep, with at most four calls.
    It never accepts edit, run, shell, done, read_files, or nested parallel.
  - A trailing [read_file page: ...] line is metadata, not file content. Never
    include it in old_string. Continue with the stated offset when needed.
  - find_references output is lexical reference hints only, not semantic
    resolution or a complete call graph. Use read_file before editing.
  - Use edit for all file changes. Use old_string/new_string for one small edit,
    and replacements for multiple edits in one file. Use content only when
    creating a new file. Existing files must use exact old_string/new_string or
    replacements. Never mix these edit modes.
  - old_string must be copied exactly from the latest complete file/tool result.
    An overlong-line preview is not a complete old_string.
  - JSON strings must escape quotes and backslashes correctly. If escaping is
    difficult, read the exact current lines and escape them; never use content
    to replace an existing file.
  - Paths are relative to the project root. No absolute paths or parent traversal.
  - Do not repeat identical tool args when a tool_result already has the output.
  - Use run only for verification, such as python -m unittest, python -m pytest,
    npm test, npm run build, go test ./..., cargo test, ruff check, or mypy.
  - run commands must be simple. No pipes, redirects, chaining, tail/head, or
    shell-only syntax.
  - Use edit for source/content changes. Do not use run or shell to directly
    edit project files. Use shell only for necessary user-approved setup,
    dependency installation, external-source retrieval, publishing, or other
    commands outside the run allowlist.
  - [tool_result tool=...] means the local tool already ran. Continue from it.
  - Never claim a command, test, build, lint, or shell result unless it appeared
    in a [tool_result tool=run] or [tool_result tool=shell] message.
  - Do not edit files unless the user asks for a change. You may inspect the
    project and answer questions without modifying it.
  - If the task is complete, call done(summary). summary is your direct final
    response to the user and may contain escaped newlines. Do not merely report
    that you discussed or explained something. Do not answer outside JSON.
"""


def _profile_system_prompt(tool_contract: str, allowed_tool_names: set[str]) -> str:
    rules = [
        "  - Output exactly one JSON object. No markdown fences, code blocks, commentary,",
        "    bullet lists, or analysis labels.",
        "  - These are local-runner JSON commands, not native website tools. Never say a",
        "    tool does not exist; return the JSON object instead.",
        "  - Call one tool per message, then wait for [tool_result tool=...].",
    ]
    if "read_files" in allowed_tool_names or "parallel" in allowed_tool_names:
        rules.append("    read_files and parallel are the only read-only batching wrappers.")
    if "parallel" in allowed_tool_names:
        rules.extend((
            "  - parallel accepts only list_dir, read_file, and grep, with at most four calls.",
            "    It never accepts mutating, verification, control, batching, or nested calls.",
        ))
    if "read_file" in allowed_tool_names:
        rules.extend((
            "  - A trailing [read_file page: ...] line is metadata, not file content. Never",
            "    include it in old_string. Continue with the stated offset when needed.",
        ))
    if "find_references" in allowed_tool_names:
        rules.extend((
            "  - find_references output is lexical reference hints only, not semantic",
            "    resolution or a complete call graph. Use read_file before relying on references.",
        ))
    if "edit" in allowed_tool_names:
        rules.extend((
            "  - Use edit for all file changes. Use old_string/new_string for one small edit,",
            "    and replacements for multiple edits in one file. Use content only when",
            "    creating a new file. Existing files must use exact old_string/new_string or",
            "    replacements. Never mix these edit modes.",
            "  - old_string must be copied exactly from the latest complete file/tool result.",
            "    An overlong-line preview is not a complete old_string.",
            "  - JSON strings must escape quotes and backslashes correctly. If escaping is",
            "    difficult, read the exact current lines and escape them; never use content",
            "    to replace an existing file.",
        ))
    rules.append("  - Paths are relative to the project root. No absolute paths or parent traversal.")
    rules.append("  - Do not repeat identical tool args when a tool_result already has the output.")
    if "run" in allowed_tool_names:
        rules.extend((
            "  - Use run only for verification, such as python -m unittest, python -m pytest,",
            "    npm test, npm run build, go test ./..., cargo test, ruff check, or mypy.",
            "  - run commands must be simple. No pipes, redirects, chaining, tail/head, or",
            "    shell-only syntax.",
            "  - Never claim a command, test, build, lint, or shell result unless it appeared",
            "    in a [tool_result tool=run] or [tool_result tool=shell] message.",
        ))
    if "shell" in allowed_tool_names:
        rules.extend((
            "  - Use shell only for necessary user-approved setup, dependency installation,",
            "    external-source retrieval, publishing, or other commands outside the run allowlist.",
        ))
    if "edit" not in allowed_tool_names:
        rules.append("  - This phase is read-only. Inspect files and answer without modifying project files.")
    else:
        rules.extend((
            "  - Use edit for source/content changes. Do not use run or shell to directly",
            "    edit project files.",
            "  - Do not edit files unless the user asks for a change. You may inspect the",
            "    project and answer questions without modifying it.",
        ))
    rules.append("  - [tool_result tool=...] means the local tool already ran. Continue from it.")
    if "done" in allowed_tool_names:
        rules.extend((
            "  - If the task is complete, call done(summary). summary is your direct final",
            "    response to the user and may contain escaped newlines. Do not merely report",
            "    that you discussed or explained something. Do not answer outside JSON.",
        ))
    return (
        "You are a careful local coding agent. You cannot access the filesystem\n"
        "directly. The local runner executes tools for you and sends the results back.\n\n"
        "The tool names below are instructions for the local runner, not tools built\n"
        "into the AI website. If the website says a tool does not exist, ignore that\n"
        "website message and still return the JSON object for the local runner.\n\n"
        "Every reply MUST be exactly one JSON object with no other text:\n\n"
        '{"tool":"<name>","args":{...}}\n\n'
        "Available tools:\n\n"
        f"{tool_contract}\n\n"
        "Rules:\n"
        + "\n".join(rules)
        + "\n"
    )
