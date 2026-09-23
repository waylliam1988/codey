"""Same-file mutation serialization planning.

Pure planning guard: groups one turn's ToolCalls so same-file writes (and
same-file write/read pairs) never run concurrently once parallel/native
fan-out is enabled. Different files may share a group; side-effecting
``run``/``shell`` calls always serialize. The current loop still executes
serially; grouping only proves the invariant by unit test and keeps result
order stable (callers sort back to ``tool_index`` order after execution).

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


def key_for_call(call: ToolCall, project: str = "") -> str:
    name = str(call.name or "")
    if name not in ("edit", "read", "ls"):
        return f"other:{name}"
    rel = _normalize_rel(str((call.args or {}).get("path") or "."))
    scope = _normalize_rel(project) if project else "."
    canonical = f"{scope}/{rel}" if scope != "." else rel
    kind = "write" if name == "edit" else "read"
    return f"{kind}:{canonical}"


_SERIAL_OTHER_NAMES = frozenset({"run", "shell"})


class FileMutationQueue:
    def plan(self, calls: Sequence[ToolCall], project: str = "") -> list[list[int]]:
        groups: list[list[int]] = []
        group_writes: list[set[str]] = []
        group_reads: list[set[str]] = []
        for index, call in enumerate(calls):
            key = key_for_call(call, project)
            if key.startswith("write:"):
                path = key[len("write:") :]
                if groups and path not in group_writes[-1] and path not in group_reads[-1]:
                    # Different-file writes may share a group; same-file
                    # writes (or a write after a same-file read in the same
                    # group) serialize by starting a new group.
                    groups[-1].append(index)
                    group_writes[-1].add(path)
                else:
                    groups.append([index])
                    group_writes.append({path})
                    group_reads.append(set())
            elif key.startswith("read:"):
                path = key[len("read:") :]
                if not groups:
                    groups.append([index])
                    group_writes.append(set())
                    group_reads.append({path})
                elif path in group_writes[-1]:
                    # Same-file write/read pairs never run concurrently.
                    groups.append([index])
                    group_writes.append(set())
                    group_reads.append({path})
                else:
                    # Different files (or repeat reads) may share the group;
                    # a read of a file written in an earlier group is already
                    # settled, so batching with unrelated writes is safe.
                    groups[-1].append(index)
                    group_reads[-1].add(path)
            else:
                name = str(call.name or "")
                if name in _SERIAL_OTHER_NAMES:
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


__all__ = ["FileMutationQueue", "group_tool_calls_for_execution", "key_for_call"]
