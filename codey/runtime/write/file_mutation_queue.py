"""Mutation serialization planning over explicit scopes.

Pure planning guard: groups one turn's ToolCalls so conflicting scopes never
run concurrently once parallel/native fan-out is enabled. ``scope_for_call``
is the single classifier (write/read/serial/other); the planner only reasons
about scope conflicts. The current loop still executes serially; grouping
only proves the invariant by unit test and keeps result order stable
(callers sort back to ``tool_index`` order after execution).

Stays dependency-free on purpose: ``runtime`` must not import ``agents``
(architecture boundary). Paths are normalized lexically, never resolved
against the filesystem here; execution still does full safe_join checks.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence

from codey.runtime.core.models import ToolCall


def _normalize_rel(path: str) -> str:
    text = str(path or ".").replace("\\", "/").strip()
    if not text:
        return "."
    normalized = posixpath.normpath(text)
    while normalized.startswith("../"):
        normalized = normalized[3:]
    if normalized in ("", "."):
        return "."
    return normalized.lstrip("./") or "."


def _canonical_path(call: ToolCall, project: str = "") -> str:
    rel = _normalize_rel(str((call.args or {}).get("path") or "."))
    scope = _normalize_rel(project) if project else "."
    return f"{scope}/{rel}" if scope != "." else rel


def scope_for_call(call: ToolCall, project: str = "") -> tuple[str, str]:
    """Classify one call into (scope, key) for conflict planning.

    Scopes: ``write`` (edit), ``read`` (read/ls/search/references),
    ``serial`` (run/shell side effects), ``other`` (everything else).
    """
    name = str(call.name or "")
    if name == "edit":
        return ("write", _canonical_path(call, project))
    if name in ("read", "ls", "search", "references"):
        return ("read", _canonical_path(call, project))
    if name in ("run", "shell"):
        return ("serial", name)
    return ("other", name)


def key_for_call(call: ToolCall, project: str = "") -> str:
    scope, key = scope_for_call(call, project)
    return f"{scope}:{key}"


class FileMutationQueue:
    def plan(self, calls: Sequence[ToolCall], project: str = "") -> list[list[int]]:
        groups: list[list[int]] = []
        group_writes: list[set[str]] = []
        group_reads: list[set[str]] = []
        for index, call in enumerate(calls):
            scope, key = scope_for_call(call, project)
            if scope == "write":
                if groups and key not in group_writes[-1] and key not in group_reads[-1]:
                    # Different-file writes may share a group; same-file
                    # writes (or a write after a same-file read in the same
                    # group) serialize by starting a new group.
                    groups[-1].append(index)
                    group_writes[-1].add(key)
                else:
                    groups.append([index])
                    group_writes.append({key})
                    group_reads.append(set())
            elif scope == "read":
                if not groups:
                    groups.append([index])
                    group_writes.append(set())
                    group_reads.append({key})
                elif key in group_writes[-1]:
                    # Same-file write/read pairs never run concurrently.
                    groups.append([index])
                    group_writes.append(set())
                    group_reads.append({key})
                else:
                    # Different files (or repeat reads) may share the group;
                    # a read of a file written in an earlier group is already
                    # settled, so batching with unrelated writes is safe.
                    groups[-1].append(index)
                    group_reads[-1].add(key)
            elif scope == "serial":
                groups.append([index])
                group_writes.append(set())
                group_reads.append(set())
            elif groups:
                groups[-1].append(index)
                while len(group_writes) < len(groups):
                    group_writes.append(set())
                    group_reads.append(set())
            else:
                groups.append([index])
                group_writes.append(set())
                group_reads.append(set())
        return groups


def group_tool_calls_for_execution(
    calls: Sequence[ToolCall], project: str = ""
) -> list[list[int]]:
    return FileMutationQueue().plan(calls, project)


__all__ = ["FileMutationQueue", "group_tool_calls_for_execution", "key_for_call", "scope_for_call"]
