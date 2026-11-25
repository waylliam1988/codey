"""Project current verification evidence from owned execution and read receipts."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from codey.agents.protocol import canonical_project_path
from codey.codemode.verification import project_check_choices
from codey.completion.verification_policy import _command_family
from codey.knowledge.model import canonical_json
from codey.operations.kernel.kernel_protocol import TurnSnapshot
from codey.operations.progress_comparison import ProjectCheckComparison
from codey.operations.project.edit_base import (
    bound_symbol_retry,
    current_definition,
    is_compact_symbol_retry,
    unchanged_definition,
)
from codey.operations.reference_handles import ReferenceHandleError, ReferenceHandleResolver
from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, JsonValue, require_json_integer
from codey.workspace.paths import path_hash, safe_join


def _read_window(session: TaskSession, resolver: ReferenceHandleResolver, project: Path,
                 path: str, line: int) -> JsonObject | None:
    reads = [(ref, receipt) for ref, receipt in reversed(tuple(session._memory_results.items()))
             if receipt.ok and receipt.call.name in {"read_file", "read"}
             and isinstance(receipt.call.args.get("path"), str)]
    if not reads:
        return None
    try:
        version = "raw:" + path_hash(safe_join(project, path), preserve_newlines=True)
    except (OSError, ValueError):
        return None
    for ref, receipt in reads:
        raw_path = receipt.call.args.get("path")
        value = receipt.canonical.get("value")
        if (not isinstance(raw_path, str) or receipt.canonical.get("file_version") != version
                or not isinstance(value, dict) or value.get("text_complete") is not True):
            continue
        start, end, content = value.get("start_line"), value.get("end_line"), value.get("text")
        if type(start) is not int or type(end) is not int or not isinstance(content, str) or not start <= line <= end:
            continue
        try:
            if canonical_project_path(project, raw_path) != canonical_project_path(project, path):
                continue
            handle = resolver.handle_for(ref)
            resolver.resolve(handle)
        except (OSError, ValueError):
            continue
        lines = content.split("\n")
        first = max(start, line - 2)
        last = min(end, first + 6)
        return {"start": first, "lines": [text[:200] for text in lines[first - start:last - start + 1]],
                "file_version": version, "result_ref": handle}
    return None


def _output_action(resolver: ReferenceHandleResolver, snapshot: TurnSnapshot,
                   fact: ProjectCheckComparison) -> JsonObject | None:
    if "read_tool_result" in snapshot.tool_names:
        try:
            handle = resolver.handle_for(fact.execution_ref)
            resolver.resolve(handle)
            return {"tool": "read_tool_result", "args": {"result_ref": handle}}
        except ReferenceHandleError:
            pass
    if "task_output" in snapshot.tool_names:
        return {"tool": "task_output", "args": {"task_ref": fact.task_ref}}
    return None


def _inspection(session: TaskSession, snapshot: TurnSnapshot, project: Path, resolver: ReferenceHandleResolver,
                frame: JsonObject) -> JsonObject:
    path = str(frame["path"])
    line = require_json_integer(frame["line"], "failure line")
    if (window := _read_window(session, resolver, project, path, line)) is not None:
        return {"frame": frame, "current_read": window}
    if "read_file" in snapshot.tool_names:
        return {"frame": frame, "read": {"tool": "read_file",
                "args": {"path": path, "offset": max(1, line - 2), "limit": 7}}}
    return {"frame": frame}


def _selected_observations(choices: tuple[tuple[JsonObject, JsonObject], ...],
                           facts: dict[tuple[str, str], ProjectCheckComparison],
                           positions: dict[str, int]) -> list[tuple[ProjectCheckComparison, JsonObject | None, bool]]:
    results: list[tuple[ProjectCheckComparison, JsonObject | None, bool]] = []
    emitted: set[str] = set()
    for args, rerun in choices:
        command, cwd = args.get("command"), args.get("path", ".")
        if not isinstance(command, str) or not isinstance(cwd, str):
            continue
        if (exact := facts.get((command, cwd))) is not None:
            results.append((exact, rerun, True))
            emitted.add(exact.execution_ref)
        family = _command_family(command)
        after = positions[exact.execution_ref] if exact is not None else -1
        candidates = [fact for fact in facts.values() if fact.cwd == cwd and fact.exit_code != 0
                      and _command_family(fact.command) == family
                      and fact.execution_ref not in emitted and positions[fact.execution_ref] > after]
        if candidates:
            extra = candidates[-1]
            results.append((extra, rerun, False))
            emitted.add(extra.execution_ref)
    if not choices and facts:
        results.append((next(reversed(facts.values())), None, False))
    return results


def project_check_handoff(session: TaskSession, snapshot: TurnSnapshot, project: Path,
                          compare: Callable[[ToolResult], ProjectCheckComparison | None]) -> str:
    if not session.workspace_fingerprint or not session.policy.allows("project.verify"):
        return ""
    spec = next((tool for tool in snapshot.frozen_specs if tool.name == "run"), None)
    choices = project_check_choices(spec) if spec is not None else ()
    if not choices and not is_compact_symbol_retry(snapshot):
        return ""
    history = session._memory_results
    positions = {ref: index for index, ref in enumerate(history)}
    facts: dict[tuple[str, str], ProjectCheckComparison] = {}
    for receipt in history.values():
        fact = compare(receipt)
        if fact is None:
            continue
        key = fact.command, fact.cwd
        previous = facts.get(key)
        if previous is None or positions[fact.execution_ref] > positions[previous.execution_ref]:
            facts[key] = fact
    if not facts:
        return ""
    def execution_order(row: tuple[tuple[str, str], ProjectCheckComparison]) -> int:
        return positions[row[1].execution_ref]

    facts = dict(sorted(facts.items(), key=execution_order))
    selected = _selected_observations(choices, facts, positions)
    if not selected:
        return ""
    resolver = ReferenceHandleResolver(session)
    last_edit = next((result for result in reversed(tuple(history.values()))
                      if result.ok and result.call.name == "edit" and type(result.audit.get("changed")) is bool), None)
    source_only = bound_symbol_retry(snapshot) is not None
    observations: list[JsonValue] = []
    for fact, rerun, exact in selected:
        current = (fact.workspace_revision, fact.workspace_fingerprint) == (
            session.workspace_revision, session.workspace_fingerprint)
        row: JsonObject = {"command": fact.command, "cwd": fact.cwd, "execution_ref": fact.execution_ref,
                           "workspace_revision": fact.workspace_revision, "exit_code": fact.exit_code,
                           "selected_check": exact}
        if (output := _output_action(resolver, snapshot, fact)) is not None:
            row["read_check_output"] = output
        if not current:
            row["state"] = "needs_verification"
            if rerun is not None and "run" in snapshot.tool_names:
                row["verify"] = rerun
        elif fact.exit_code == 0:
            row["state"] = "passed"
        else:
            row["state"] = "failed"
            row["failed_tests"] = list(fact.failure_ids[:8])
            row["failure_list_complete"] = fact.complete and len(fact.failure_ids) <= 8
            row["locations"] = [_inspection(session, snapshot, project, resolver, frame) for frame in fact.frames]
            if not source_only and last_edit is not None and last_edit.audit.get("changed") is False:
                row["unchanged_edit"] = last_edit.call.args.get("path")
                row["next"] = "Inspect the failing assertion and the current file before repeating the check."
                if (target := unchanged_definition(last_edit)) is not None:
                    path, name, old = target
                    symbol = current_definition(session, project, path, name, old)
                    if "edit" in snapshot.tool_names and symbol:
                        row["edit_definition"] = {"tool": "edit", "args": {"path": path, "symbol": symbol},
                                                  "add_content": "Supply one updated Python definition."}
                    elif "read_file" in snapshot.tool_names and symbol is None:
                        row["read_edit_base"] = {"tool": "read_file", "args": {"path": path}}
        observations.append(row)
    return "Project check evidence:\n" + canonical_json(observations)
