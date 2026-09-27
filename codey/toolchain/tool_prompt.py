"""Coding tool contract rendering for the model-visible prompt surface.

Pure prompt helpers only. No imports of agent, runtime executors, providers,
task runners, ghost, or browser.
"""

from __future__ import annotations

import hashlib
import json


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


def coding_model_tool_contract_hash(
    definitions: tuple[object, ...] | None = None,
) -> str:
    return model_visible_contract_hash(
        "coding_tool_contract",
        render_coding_tool_contract_text(definitions),  # type: ignore[arg-type]
    )


_WRITER_TOOL_NAMES = frozenset({
    "list_dir",
    "read_file",
    "read_files",
    "grep",
    "find_references",
    "parallel",
    "edit",
    "run",
    "shell",
    "done",
})

# Single-source rule fragments: identical lines live exactly once here and are
# reused by both the frozen writer sequence and the profile builder. Only the
# capability-dependent wording (batching join, parallel scope, find_references
# tail, edit/shell merge, Never-claim position) stays writer/profile-specific
# so the golden fixtures remain byte-identical.
_RULE_OUTPUT_JSON = (
    "  - Output exactly one JSON object. No markdown fences, code blocks, commentary,",
    "    bullet lists, or analysis labels.",
)
_RULE_NATIVE_DENIAL = (
    "  - These are local-runner JSON commands, not native website tools. Never say a",
    "    tool does not exist; return the JSON object instead.",
)
_RULE_READFILE_TRAILING = (
    "  - A trailing [read_file page: ...] line is metadata, not file content. Never",
    "    include it in old_string. Continue with the stated offset when needed.",
)
_RULE_EDIT_MODES = (
    "  - Use edit for all file changes. Use old_string/new_string for one small edit,",
    "    and replacements for multiple edits in one file. Use content only when",
    "    creating a new file. Existing files must use exact old_string/new_string or",
    "    replacements. Never mix these edit modes.",
)
_RULE_OLD_STRING = (
    "  - old_string must be copied exactly from the latest complete file/tool result.",
    "    An overlong-line preview is not a complete old_string.",
)
_RULE_JSON_ESCAPE = (
    "  - JSON strings must escape quotes and backslashes correctly. If escaping is",
    "    difficult, read the exact current lines and escape them; never use content",
    "    to replace an existing file.",
)
_RULE_EDIT_BLOCK = _RULE_EDIT_MODES + _RULE_OLD_STRING + _RULE_JSON_ESCAPE
_RULE_PATHS = (
    "  - Paths are relative to the project root. No absolute paths or parent traversal.",
)
_RULE_NO_REPEAT = (
    "  - Do not repeat identical tool args when a tool_result already has the output.",
)
_RULE_RUN_VERIFICATION = (
    "  - Use run only for verification, such as python -m unittest, python -m pytest,",
    "    npm test, npm run build, go test ./..., cargo test, ruff check, or mypy.",
    "  - run commands must be simple. No pipes, redirects, chaining, tail/head, or",
    "    shell-only syntax.",
)
_RULE_NEVER_CLAIM = (
    "  - Never claim a command, test, build, lint, or shell result unless it appeared",
    "    in a [tool_result tool=run] or [tool_result tool=shell] message.",
)
_RULE_TOOL_RESULT = (
    "  - [tool_result tool=...] means the local tool already ran. Continue from it.",
)
_RULE_DONE = (
    "  - If the task is complete, call done(summary). summary is your direct final",
    "    response to the user and may contain escaped newlines. Do not merely report",
    "    that you discussed or explained something. Do not answer outside JSON.",
)
_RULE_DO_NOT_EDIT = (
    "  - Do not edit files unless the user asks for a change. You may inspect the",
    "    project and answer questions without modifying it.",
)
_RULE_READONLY = (
    "  - This phase is read-only. Inspect files and answer without modifying project files.",
)

# Writer-specific wording (frozen by golden fixtures).
_RULE_WRITER_BATCHING = (
    "  - Call one tool per message, then wait for [tool_result tool=...]. read_files",
    "    and parallel are the only read-only batching wrappers.",
)
_RULE_WRITER_PARALLEL = (
    "  - parallel accepts only list_dir, read_file, and grep, with at most four calls.",
    "    It never accepts edit, run, shell, done, read_files, or nested parallel.",
)
_RULE_WRITER_FIND_REFERENCES = (
    "  - find_references output is lexical reference hints only, not semantic",
    "    resolution or a complete call graph. Use read_file before editing.",
)
_RULE_WRITER_EDIT_SHELL_COMBINED = (
    "  - Use edit for source/content changes. Do not use run or shell to directly",
    "    edit project files. Use shell only for necessary user-approved setup,",
    "    dependency installation, external-source retrieval, publishing, or other",
    "    commands outside the run allowlist.",
)

# Byte-frozen writer rules assembled from the single-source fragments above.
# Do not reword without updating the golden files.
_WRITER_RULE_LINES = (
    *_RULE_OUTPUT_JSON,
    *_RULE_NATIVE_DENIAL,
    *_RULE_WRITER_BATCHING,
    *_RULE_WRITER_PARALLEL,
    *_RULE_READFILE_TRAILING,
    *_RULE_WRITER_FIND_REFERENCES,
    *_RULE_EDIT_BLOCK,
    *_RULE_PATHS,
    *_RULE_NO_REPEAT,
    *_RULE_RUN_VERIFICATION,
    *_RULE_WRITER_EDIT_SHELL_COMBINED,
    *_RULE_TOOL_RESULT,
    *_RULE_NEVER_CLAIM,
    *_RULE_DO_NOT_EDIT,
    *_RULE_DONE,
)


def _render_preface(tool_contract: str) -> str:
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
    )


def _normalize_tool_names(names: set[str] | frozenset[str]) -> set[str]:
    return {str(name or "").strip().lower() for name in names if str(name or "").strip()}


def _defined_tool_names(definitions: tuple[object, ...]) -> set[str]:
    defined: set[str] = set()
    for definition in definitions:
        name = str(getattr(definition, "name", "") or "").strip().lower()
        if name:
            defined.add(name)
    return defined


def _is_full_writer_toolset(allowed_tool_names: set[str]) -> bool:
    return _normalize_tool_names(allowed_tool_names) == set(_WRITER_TOOL_NAMES)


def render_coding_system_prompt(
    definitions: tuple[object, ...],
    *,
    profile_name: str,
    allowed_tool_names: set[str],
) -> str:
    """Single prompt entry: rules always follow ``allowed_tool_names``.

    ``profile_name`` is kept for API compatibility but no longer selects a
    separate concatenation path. The full writer toolset reproduces the
    frozen writer text verbatim (see golden fixtures); any restricted set
    follows the profile rule builder so a ``coding_writer`` label can never
    smuggle writer-only rules when the tools are not actually allowed.

    ``allowed_tool_names`` must exactly match the tool names in
    ``definitions`` (the model-visible contract). A mismatch raises
    ``ValueError`` instead of silently emitting rules for tools the contract
    does not show (or vice versa).
    """

    del profile_name
    tool_contract = render_coding_tool_contract_text(definitions)  # type: ignore[arg-type]
    allowed = _normalize_tool_names(set(allowed_tool_names or set()))
    defined = _defined_tool_names(tuple(definitions or ()))
    if allowed != defined:
        raise ValueError(
            "allowed_tool_names must match definitions: "
            f"allowed={sorted(allowed)} defined={sorted(defined)}"
        )
    if _is_full_writer_toolset(allowed):
        return _render_preface(tool_contract) + "\n".join(_WRITER_RULE_LINES) + "\n"
    return _profile_system_prompt(tool_contract, allowed)


def _profile_system_prompt(tool_contract: str, allowed_tool_names: set[str]) -> str:
    rules: list[str] = []
    rules.extend(_RULE_OUTPUT_JSON)
    rules.extend(_RULE_NATIVE_DENIAL)
    rules.append("  - Call one tool per message, then wait for [tool_result tool=...].")
    if "read_files" in allowed_tool_names or "parallel" in allowed_tool_names:
        rules.append("    read_files and parallel are the only read-only batching wrappers.")
    if "parallel" in allowed_tool_names:
        rules.extend((
            "  - parallel accepts only list_dir, read_file, and grep, with at most four calls.",
            "    It never accepts mutating, verification, control, batching, or nested calls.",
        ))
    if "read_file" in allowed_tool_names:
        rules.extend(_RULE_READFILE_TRAILING)
    if "find_references" in allowed_tool_names:
        rules.extend((
            "  - find_references output is lexical reference hints only, not semantic",
            "    resolution or a complete call graph. Use read_file before relying on references.",
        ))
    if "edit" in allowed_tool_names:
        rules.extend(_RULE_EDIT_BLOCK)
    rules.extend(_RULE_PATHS)
    rules.extend(_RULE_NO_REPEAT)
    if "run" in allowed_tool_names:
        rules.extend(_RULE_RUN_VERIFICATION)
        rules.extend(_RULE_NEVER_CLAIM)
    if "shell" in allowed_tool_names:
        rules.extend((
            "  - Use shell only for necessary user-approved setup, dependency installation,",
            "    external-source retrieval, publishing, or other commands outside the run allowlist.",
        ))
    if "edit" not in allowed_tool_names:
        rules.extend(_RULE_READONLY)
    else:
        rules.extend((
            "  - Use edit for source/content changes. Do not use run or shell to directly",
            "    edit project files.",
        ))
        rules.extend(_RULE_DO_NOT_EDIT)
    rules.extend(_RULE_TOOL_RESULT)
    if "done" in allowed_tool_names:
        rules.extend(_RULE_DONE)
    return _render_preface(tool_contract) + "\n".join(rules) + "\n"
