"""Same-file mutation serialization planning.

Pure planning guard: groups one turn's ToolCalls so same-file edits (and
same-file edit/read pairs) never run concurrently once parallel/native
fan-out is enabled. The current loop still executes serially; this only
proves the grouping invariant by unit test.

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


class FileMutationQueue:
    def plan(self, calls: Sequence[ToolCall], project: str = "") -> list[list[int]]:
        groups: list[list[int]] = []
        write_keys: set[str] = set()
        for index, call in enumerate(calls):
            key = key_for_call(call, project)
            if key.startswith("write:"):
                path = key[len("write:"):]
                # Each write gets its own group so same-file edits serialize.
                write_keys.add(path)
                groups.append([index])
            elif key.startswith("read:"):
                path = key[len("read:"):]
                if path in write_keys:
                    groups.append([index])
                elif groups:
                    groups[-1].append(index)
                else:
                    groups.append([index])
            else:
                if groups:
                    groups[-1].append(index)
                else:
                    groups.append([index])
        return groups


def group_tool_calls_for_execution(
    calls: Sequence[ToolCall], project: str = ""
) -> list[list[int]]:
    return FileMutationQueue().plan(calls, project)


__all__ = ["FileMutationQueue", "group_tool_calls_for_execution", "key_for_call"]
