from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from codey.codemode.types import ToolCatalogEntry, ToolCatalogSnapshot


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def build_catalog_snapshot(rows: Iterable[Mapping[str, Any] | ToolCatalogEntry]) -> ToolCatalogSnapshot:
    entries: list[ToolCatalogEntry] = []
    for row in rows:
        entry = row if isinstance(row, ToolCatalogEntry) else ToolCatalogEntry(
            name=str(row.get("name", "")),
            description=str(row.get("description", "")),
            parameters=row.get("parameters", {}) if isinstance(row.get("parameters", {}), Mapping) else {},
            namespace=str(row.get("namespace", "") or ""),
            read_only=bool(row.get("read_only", True)),
            parallel_safe=bool(row.get("parallel_safe", False)),
        )
        if entry.name in {"codemode", "done", "ask_user"}:
            continue
        entries.append(entry)
    entries.sort(key=lambda item: item.name)
    unique: list[ToolCatalogEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.name in seen:
            raise ValueError(f"duplicate catalog tool: {entry.name}")
        seen.add(entry.name)
        unique.append(entry)
    payload = [
        {
            "name": entry.name,
            "description": entry.description,
            "parameters": entry.parameters,
            "namespace": entry.namespace,
            "read_only": entry.read_only,
            "parallel_safe": entry.parallel_safe,
        }
        for entry in unique
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return ToolCatalogSnapshot(tuple(unique), digest)


def build_catalog_from_specs(specs: Mapping[str, Any] | Iterable[Any]) -> ToolCatalogSnapshot:
    rows: list[dict[str, object]] = []
    values = specs.values() if isinstance(specs, Mapping) else specs
    for spec in values:
        name = str(getattr(spec, "name", "") or "").strip()
        if not name:
            continue
        executor = str(getattr(spec, "executor", "") or "")
        rows.append({
            "name": name,
            "description": str(getattr(spec, "description", "") or ""),
            "parameters": {
                key: dict(schema) if isinstance(schema, Mapping) else {"type": "string"}
                for key, schema in (getattr(spec, "parameters", ()) or ())
            },
            "namespace": executor,
            "read_only": str(getattr(spec, "replay_class", "unsafe")) == "safe",
            "parallel_safe": str(getattr(spec, "replay_class", "unsafe")) == "safe",
        })
    return build_catalog_snapshot(rows)


def build_read_only_catalog(specs: Mapping[str, Any] | Iterable[Any]) -> ToolCatalogSnapshot:
    values = specs.values() if isinstance(specs, Mapping) else specs
    safe = [
        spec
        for spec in values
        if str(getattr(spec, "replay_class", "unsafe") or "unsafe") == "safe"
        and str(getattr(spec, "executor", "") or "") in {"project", "source", "knowledge"}
        and str(getattr(spec, "name", "") or "") not in {"parallel", "read_files"}
    ]
    snapshot = build_catalog_from_specs(safe)
    entries = tuple(
        ToolCatalogEntry(
            name=entry.name,
            description=entry.description,
            parameters=entry.parameters,
            namespace=entry.namespace,
            read_only=True,
            parallel_safe=entry.name in {"list_dir", "read_file", "grep"},
        )
        for entry in snapshot.entries
    )
    return ToolCatalogSnapshot(entries, snapshot.digest)


def render_declarations(snapshot: ToolCatalogSnapshot) -> str:
    lines = [
        "// Frozen CodeMode tool declarations. Calls are host mediated.",
        "declare const tools: {",
    ]
    for entry in snapshot.entries:
        schema = json.dumps(_json_value(entry.parameters), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lines.append(f"  // tools.{entry.name}: {schema}")
        lines.append(f"  {entry.name}: (args: Record<string, unknown>) => Promise<unknown>;")
    lines.extend(("};", f"// catalog_digest={snapshot.digest}"))
    return "\n".join(lines)
