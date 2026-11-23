"""Bind directory instructions presented for a turn to its project writes."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from codey.agents.context import (
    PROJECT_INSTRUCTION_FILES,
    PROJECT_INSTRUCTIONS_CONTEXT_BUDGET,
    ProjectInstruction,
    load_project_instructions,
)
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from codey.utils.refs import digest_text
from codey.workspace.paths import safe_join

INSTRUCTION_CHANGE = "project_instructions_changed"


@dataclass(frozen=True)
class ProjectInstructionBinding:
    name: str
    scope_dir: str
    content_digest: str
    delivered_digest: str

    def to_payload(self) -> JsonObject:
        return {"name": self.name, "scope_dir": self.scope_dir, "content_digest": self.content_digest,
                "delivered_digest": self.delivered_digest}

    @classmethod
    def from_payload(cls, raw: object) -> ProjectInstructionBinding:
        row = decode_json_object(raw)
        if set(row) != {"name", "scope_dir", "content_digest", "delivered_digest"}:
            raise ValueError("invalid captured project instruction")
        name = require_json_string(row["name"], "instruction path")
        scope = require_json_string(row["scope_dir"], "instruction scope")
        content = require_json_string(row["content_digest"], "instruction digest")
        delivered = require_json_string(row["delivered_digest"], "delivered digest")
        path = PurePosixPath(name)
        if (path.as_posix() != name or path.is_absolute() or ":" in name or "\\" in name or ".." in path.parts
                or path.name not in PROJECT_INSTRUCTION_FILES or path.parent.as_posix() != scope
                or re.fullmatch(r"sha256:[0-9a-f]{64}", content) is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", delivered) is None):
            raise ValueError("invalid captured project instruction identity")
        return cls(name, scope, content, delivered)


def _complete_instruction_reads(root: Path, receipts: Mapping[str, ToolResult]) -> dict[str, tuple[tuple[str, ...], str]]:
    pages: dict[tuple[str, str, int], dict[int, tuple[int, str, str]]] = {}
    resolved_root = root.resolve()
    for ref, result in receipts.items():
        path = result.call.args.get("path")
        value = result.canonical.get("value")
        version = result.canonical.get("file_version")
        if (result.call.name != "read_file" or not result.ok or not isinstance(path, str)
                or Path(path).name not in PROJECT_INSTRUCTION_FILES or not isinstance(version, str)
                or not isinstance(value, dict) or value.get("text_complete") is not True
                or not isinstance(body := value.get("text"), str) or body not in result.model_text):
            continue
        start, end, total = value.get("start_line"), value.get("end_line"), value.get("total_lines")
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(total, int):
            continue
        try:
            name = safe_join(resolved_root, path).relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            continue
        pages.setdefault((name, version, total), {})[start] = (end, body, ref)
    reads: dict[str, tuple[tuple[str, ...], str]] = {}
    for (name, _version, total), spans in pages.items():
        position = 1
        bodies: list[str] = []
        refs: list[str] = []
        while (page := spans.get(position)) is not None:
            end, body, ref = page
            if end < position and total != 0:
                break
            bodies.append(body)
            refs.append(ref)
            position = end + 1
            if end == total:
                reads[name] = (tuple(refs), digest_text("".join(bodies)))
                break
    return reads


def instruction_handoff(root: Path, read_files: Iterable[str], receipts: Mapping[str, ToolResult],
                         ) -> tuple[tuple[ProjectInstructionBinding, ...], str]:
    targets: dict[str, None] = {}
    for result in reversed(tuple(receipts.values())):
        raw = result.canonical.get(INSTRUCTION_CHANGE)
        if isinstance(raw, list):
            targets.update(dict.fromkeys(target for target in raw if isinstance(target, str)))
    targets.update(dict.fromkeys(sorted(read_files)))
    docs: dict[str, ProjectInstruction] = {doc.name: doc for doc in load_project_instructions(root)}
    for target in targets:
        try:
            docs.update((doc.name, doc) for doc in load_project_instructions(root, target=target))
        except (OSError, ValueError):
            # An unreadable path still fails its write preflight. It must not
            # prevent other project reads and writes from reaching the model.
            continue
    if not docs:
        return (), ""
    history = _complete_instruction_reads(root, receipts)
    heading = "Current repository instructions by directory:"
    budget = PROJECT_INSTRUCTIONS_CONTEXT_BUDGET - len(heading)
    bindings: list[ProjectInstructionBinding] = []
    lines: list[str] = []
    for doc in docs.values():
        prior = history.get(doc.name)
        label = f"{doc.name} applies under {doc.scope_dir}"
        if prior is not None and prior[1] == doc.content_digest:
            line = f"{label}. Complete text was delivered in read receipts {', '.join(prior[0])}."
            delivered = doc.content_digest
        elif not doc.truncated and len(label) + len(doc.content) + 8 <= budget:
            line = f"--- {label} ---\n{doc.content}"
            delivered = digest_text(doc.content)
        else:
            preview = doc.content[:min(500, max(0, budget - len(label) - 200))]
            line = f"--- {label} (partial) ---\n{preview}\nRead {doc.name} in full before writing under {doc.scope_dir}."
            delivered = digest_text(preview)
        if len(line) + 2 > budget:
            continue
        budget -= len(line) + 2
        lines.append(line)
        bindings.append(ProjectInstructionBinding(doc.name, doc.scope_dir, doc.content_digest, delivered))
    return tuple(bindings), heading + "\n" + "\n\n".join(lines)


def changed_instruction_target(root: Path, target: str, bindings: tuple[ProjectInstructionBinding, ...]) -> str | None:
    resolved_root = root.resolve()
    path = safe_join(resolved_root, target)
    target_key = path.relative_to(resolved_root).as_posix()
    folder = PurePosixPath(target_key).parent
    eligible = {rule.name: (rule.content_digest, rule.delivered_digest) for rule in bindings
                if folder.is_relative_to(PurePosixPath(rule.scope_dir))}
    current = {doc.name: (doc.content_digest, doc.content_digest)
               for doc in load_project_instructions(resolved_root, target=target)}
    return target_key if current != eligible else None


def record_blocked_instruction_targets(result: ToolResult, targets: set[str]) -> ToolResult:
    if not targets:
        return result
    return replace(result, canonical={**result.canonical, INSTRUCTION_CHANGE: sorted(targets)})
