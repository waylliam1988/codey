from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from codey.codemode.types import ToolCatalogEntry, ToolCatalogSnapshot
from codey.toolchain.result_schema import schema_typescript
from codey.toolchain.tool_spec import ToolSpec, is_text_batch_tool
from codey.types.json_value import canonical_json_object


def build_catalog_snapshot(rows: Iterable[Mapping[str, object] | ToolCatalogEntry]) -> ToolCatalogSnapshot:
    entries: list[ToolCatalogEntry] = []
    for row in rows:
        entry = row if isinstance(row, ToolCatalogEntry) else ToolCatalogEntry(
            name=str(row.get("name", "")),
            description=str(row.get("description", "")),
            parameters=canonical_json_object(row.get("parameters", {})),
            namespace=str(row.get("namespace", "") or ""),
            grant=str(row.get("grant", "") or ""),
            read_only=bool(row.get("read_only", True)),
            parallel_safe=bool(row.get("parallel_safe", False)),
            output_schema=canonical_json_object(row["output_schema"]),
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
            "parameters": canonical_json_object(entry.parameters),
            "output_schema": canonical_json_object(entry.output_schema),
            "namespace": entry.namespace,
            "grant": entry.grant,
            "read_only": entry.read_only,
            "parallel_safe": entry.parallel_safe,
        }
        for entry in unique
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return ToolCatalogSnapshot(tuple(unique), digest)


def build_catalog_from_specs(specs: Mapping[str, ToolSpec] | Iterable[ToolSpec]) -> ToolCatalogSnapshot:
    rows: list[dict[str, object]] = []
    values = specs.values() if isinstance(specs, Mapping) else specs
    for spec in values:
        name = spec.name.strip()
        if not name:
            continue
        executor = spec.executor
        rows.append({
            "name": name,
            "description": spec.description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: dict(schema) if isinstance(schema, Mapping) else {"type": "string"}
                    for key, schema in spec.parameters
                },
                "required": list(spec.required),
                "additionalProperties": False,
            },
            "namespace": executor,
            "grant": spec.grant,
            "read_only": spec.read_only,
            "parallel_safe": spec.parallel_safe,
            "output_schema": spec.output_schema,
        })
    return build_catalog_snapshot(rows)


def build_codemode_catalog(
    specs: Mapping[str, ToolSpec] | Iterable[ToolSpec], *, allow_writes: bool = False,
) -> ToolCatalogSnapshot:
    """Build the frozen CodeMode catalog for one permission snapshot.

    Read tools remain limited to the existing safe project/source/knowledge
    surface.  Explicit write mode adds only project ``edit`` and ``run``;
    shell is always kept behind the normal approval path and is never exposed
    directly to the Node worker.
    """
    snapshot = build_catalog_from_specs(specs)
    allowed: list[ToolCatalogEntry] = []
    for entry in snapshot.entries:
        if is_text_batch_tool(entry.name):
            continue
        if (
            (entry.read_only and entry.namespace in {"project", "source", "knowledge"})
            or (
                allow_writes
                and entry.name in {"edit", "run"}
                and entry.namespace == "project"
                and entry.grant == "project.write"
            )
        ):
            allowed.append(entry)
    return build_catalog_snapshot(allowed)


def render_declarations(snapshot: ToolCatalogSnapshot) -> str:
    lines = [
        "// tools.NAME uses NAME's declared arguments; returns Result<T>.",
        "type ToolResult<T> =",
        "  | {ok: true; value: T; result_ref?: string}",
        "  | {ok: false; error: string};",
        "type Result<T> = Promise<ToolResult<T>>;",
        "declare function unwrap<T>(result: ToolResult<T>): T;",
        "declare const tools: {",
    ]
    for entry in snapshot.entries:
        properties = canonical_json_object(entry.parameters.get("properties", {}))
        for name, schema in properties.items():
            description = canonical_json_object(schema).get("description")
            if description:
                lines.append(f"  // {entry.name}.{name}: {description}")
        value = schema_typescript(entry.output_schema)
        lines.append(f"  {entry.name}(args: {schema_typescript(entry.parameters)}): Result<{value}>;")
    lines.extend(("};", f"// catalog_digest={snapshot.digest}"))
    return "\n".join(lines)
