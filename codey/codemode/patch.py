"""Preflight an ordered set of replacement edits before any child write."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from codey.agents.protocol import canonical_project_path
from codey.agents.tool_execution import evaluate_tool_call_policy_for, policy_denied
from codey.operations.file_field_binding import file_rules
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.runtime import ToolOutcome, prepare_edit, replacement_blocks
from codey.types.json_value import JsonObject, require_json_string

MAX_PATCH_FILES = 8
PATCH_DESCRIPTION = ("For an all-files preflight, use edit({files: [{path, replacements}, ...]}). "
    "Read each file in this program first. The host selects its read version when unambiguous. "
    "You can select a different task read with base_result_ref. If a write fails, inspect the committed edits before retrying.")


def preflight_patch(
    root: Path, session: TaskSession, profile: str, files: tuple[JsonObject, ...], indices: tuple[int, ...],
    previous: Callable[[int], ToolResult | None], current_reads: tuple[str, ...],
) -> tuple[JsonObject, ...]:
    planned: list[JsonObject] = []
    paths: set[str] = set()
    for index, row in zip(indices, files, strict=True):
        if set(row) not in ({"path", "replacements"}, {"path", "base_result_ref", "replacements"}):
            raise ValueError("each patch file needs path and replacements")
        path = require_json_string(row["path"], "patch path")
        canonical = canonical_project_path(root, path)
        key = os.path.normcase(canonical)
        if key in paths:
            raise ValueError(f"duplicate patch path: {path}")
        paths.add(key)
        if file_rules(session.policy.required_checks, canonical):
            raise ValueError(f"patch path has receipt-bound fields: {path}")
        refs = (require_json_string(row["base_result_ref"], "base_result_ref"),) if "base_result_ref" in row else current_reads
        version = _read_version(root, session, canonical, refs)
        args: JsonObject = {"path": path, "replacements": row["replacements"],
                            "expected_version": version.removeprefix("raw:")}
        call = ToolCall("edit", args)
        decision, _ = evaluate_tool_call_policy_for(call, project=root, permission_profile=profile, shell=session.shell)
        if policy_denied(decision):
            raise ValueError(f"patch path is denied: {path}")
        if receipt := previous(index):
            if receipt.call.name != "edit" or receipt.call.args != args:
                raise ValueError("patch recovery changed a settled edit")
        else:
            prepared = prepare_edit(root, path, replacement_blocks(row["replacements"]))
            if isinstance(prepared, ToolOutcome):
                raise ValueError(prepared.model_text)
            if prepared.version != args["expected_version"]:
                raise ValueError(f"patch file version changed: {path}; reread before retrying")
            if prepared.syntax_regression:
                raise ValueError(f"patch would introduce invalid Python in {path}; check the replacement text")
        planned.append(args)
    return tuple(planned)


def _read_version(root: Path, session: TaskSession, canonical: str, refs: tuple[str, ...]) -> str:
    versions: set[str] = set()
    for ref in refs:
        read = session._memory_results.get(ref)
        if read is None or ref not in session.executed or not read.ok or read.call.name != "read_file":
            continue
        path = require_json_string(read.call.args.get("path"), "read path")
        version = read.canonical.get("file_version")
        if canonical_project_path(root, path) == canonical and isinstance(version, str) and version.startswith("raw:sha256:"):
            versions.add(version)
    if len(versions) != 1:
        raise ValueError(f"patch needs one read_file version for {canonical}; read it here or select its base_result_ref")
    return versions.pop()
