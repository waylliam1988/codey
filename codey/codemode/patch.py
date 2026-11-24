"""Preflight an ordered set of replacement edits before any child write."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codey.agents.protocol import canonical_project_path
from codey.agents.tool_execution import evaluate_tool_call_policy_for, policy_denied
from codey.operations.file_field_binding import file_rules
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
from codey.toolchain.runtime import ToolOutcome, prepare_edit, replacement_blocks
from codey.types.json_value import JsonObject, JsonValue, require_json_string

MAX_PATCH_FILES = 8
PATCH_DESCRIPTION = ("For an all-files preflight, use edit({files: [{path, replacements}, ...]}). "
    "Read each file in this program first. The host selects its read version when unambiguous. "
    "You can select a different task read with base_result_ref. Result.patch lists the per-file outcome. "
    "If a write fails, inspect the committed edits before retrying.")


class PatchPreflightError(ValueError):
    def __init__(self, path: str, detail: str) -> None:
        super().__init__(detail)
        self.path = path


PatchState = Literal["changed", "unchanged", "failed", "not_dispatched", "unknown"]


@dataclass(frozen=True)
class PatchChild:
    receipt: ToolResult
    result_ref: str = ""


def patch_summary(paths: tuple[str, ...], receipts: Mapping[str, PatchChild], *,
                  failed_path: str = "") -> list[JsonValue]:
    summary: list[JsonValue] = []
    for path in paths:
        state: PatchState
        row: JsonObject = {"path": path}
        child = receipts.get(path)
        if child is None:
            state = "failed" if path == failed_path else "not_dispatched"
        else:
            receipt = child.receipt
            changed = receipt.audit.get("changed")
            if receipt.canonical.get("workspace_unconfirmed") is True or receipt.ok and not isinstance(changed, bool):
                state = "unknown"
            elif not receipt.ok:
                state = "failed"
            else:
                state = "changed" if changed is True else "unchanged"
            if child.result_ref:
                row["result_ref"] = child.result_ref
        row["state"] = state
        summary.append(row)
    return summary


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
        try:
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
            _check_prepared_edit(root, path, args, previous(index))
        except (OSError, ValueError) as exc:
            raise PatchPreflightError(path, str(exc)) from exc
        planned.append(args)
    return tuple(planned)


def _check_prepared_edit(root: Path, path: str, args: JsonObject, receipt: ToolResult | None) -> None:
    if receipt is not None:
        if receipt.call.name != "edit" or receipt.call.args != args:
            raise ValueError("patch recovery changed a settled edit")
        return
    version = require_json_string(args["expected_version"], "expected_version")
    prepared = prepare_edit(root, path, replacement_blocks(args["replacements"]), expected_version=version)
    if isinstance(prepared, ToolOutcome):
        if prepared.error_code == STALE_READ_REFERENCE_CODE:
            raise ValueError(f"patch file version changed: {path}; reread before retrying")
        raise ValueError(prepared.model_text)
    if prepared.syntax_regression:
        raise ValueError(f"patch would introduce invalid Python in {path}; check the replacement text")


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
