from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.codemode.navigation import task_navigation_schema
from codey.codemode.types import (
    CatalogNamespaceSummary,
    CatalogSearchItem,
    CatalogSearchResult,
    CatalogSummary,
    ToolCatalogEntry,
    ToolCatalogSnapshot,
)
from codey.operations.browser_references import BrowserReferences, reference_spec
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.toolchain.result_schema import model_output_schema, schema_typescript
from codey.toolchain.tool_spec import ToolSpec, is_text_batch_tool
from codey.types.json_value import canonical_json_object, require_json_strings

if TYPE_CHECKING:
    from codey.operations.kernel_protocol import TurnSnapshot


def snapshot_catalog(
    snapshot: TurnSnapshot, *, allow_writes: bool = True, observed_choices: bool = True,
) -> ToolCatalogSnapshot:
    """Derive model choices or replay capabilities from the same frozen specs."""
    names = set(snapshot.nested_tool_names)
    writable = allow_writes and snapshot.policy is not None and snapshot.policy.allows("project.write")
    return build_codemode_catalog(
        (spec for spec in snapshot.frozen_specs if spec.name in names),
        allow_project_edit=writable and "edit" in names,
        allow_project_run=allow_writes and snapshot.policy is not None and snapshot.policy.allows("project.verify")
            and "run" in names,
        task_navigation_url=snapshot.task_navigation_url,
        browser_followup_checks=(snapshot.policy.required_checks if snapshot.policy else ())
        if snapshot.browser_followups else None,
        browser_references=snapshot.browser_references if observed_choices else None,
    )


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
        if entry.name in {"codemode", "done", "ask_user", "submit_research_answer", PAGE_DETAIL_TOOL}:
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
    values: Iterable[ToolSpec] = specs.values() if isinstance(specs, Mapping) else specs
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
                    key: canonical_json_object(schema) if isinstance(schema, Mapping) else {"type": "string"}
                    for key, schema in spec.parameters
                },
                "required": list(spec.required),
                "additionalProperties": False,
            },
            "namespace": executor,
            "grant": spec.grant,
            "read_only": spec.read_only,
            "parallel_safe": spec.parallel_safe,
            "output_schema": model_output_schema(canonical_json_object(spec.output_schema),
                                                 supports_background=spec.supports_background),
        })
    return build_catalog_snapshot(rows)


def build_codemode_catalog(
    specs: Mapping[str, ToolSpec] | Iterable[ToolSpec], *, allow_writes: bool = False,
    allow_project_edit: bool | None = None, allow_project_run: bool | None = None,
    task_navigation_url: str | None = None,
    browser_followup_checks: tuple[str, ...] | None = None,
    browser_references: BrowserReferences | None = None,
) -> ToolCatalogSnapshot:
    """Build the frozen CodeMode catalog for one permission snapshot.

    Read tools remain limited to the existing safe project/source/knowledge
    surface.  Explicit write mode adds project ``edit`` and optionally ``run``;
    shell is always kept behind the normal approval path and is never exposed
    directly to the Node worker.

    ``allow_writes`` remains the compatibility switch for callers that want
    both project actions.  Browser-composed surfaces can select the persistent
    edit action without implicitly exposing project verification commands.
    """
    if allow_project_edit is None:
        allow_project_edit = allow_writes
    if allow_project_run is None:
        allow_project_run = allow_writes
    source_values: Iterable[ToolSpec] = specs.values() if isinstance(specs, Mapping) else specs
    values = tuple(reference_spec(spec, browser_references) for spec in source_values)
    if browser_followup_checks is not None:
        from codey.operations.browser_followups import PURPOSES, click_plans, model_spec

        plans = click_plans(browser_followup_checks, {spec.name for spec in values})
        purposes = tuple(purpose for purpose in PURPOSES if purpose in plans)
        values = tuple(model_spec(spec, None, click_purposes=purposes)
                       if spec.name == "browser_click" else spec for spec in values)
    snapshot = build_catalog_from_specs(values)
    allowed: list[ToolCatalogEntry] = []
    for entry in snapshot.entries:
        if is_text_batch_tool(entry.name):
            continue
        if (
            (entry.read_only and entry.namespace in {"project", "source", "knowledge"}
             and entry.grant in {"project.read", "web.read", "knowledge.read"})
            or entry.namespace in {"browser", "task_control"}
            or (
                entry.namespace == "project"
                and ((entry.grant == "project.write" and allow_project_edit and entry.name == "edit")
                     or (entry.grant == "project.verify" and allow_project_run and entry.name == "run"))
            )
        ):
            if entry.name == "browser_navigate" and task_navigation_url is not None:
                entry = replace(entry, description='Use {target: "task"} to open the task URL at the chosen time. '
                    'Use {url: "..."} for other authorized pages. ' + entry.description,
                    parameters={"anyOf": [
                        task_navigation_schema(),
                        canonical_json_object(entry.parameters),
                    ]})
            allowed.append(entry)
    if browser_followup_checks is not None:
        for index, entry in enumerate(allowed):
            if entry.name == "browser_click":
                allowed[index] = replace(entry, parameters={**canonical_json_object(entry.parameters),
                                                           "x-codey-click-plans": plans})
    return build_catalog_snapshot(allowed)


_PINNED = ("read_file", "grep", "browser_snapshot", "edit")
MODEL_CATALOG_BUDGET_BYTES = 24_000


def _edit_guidance(snapshot: ToolCatalogSnapshot) -> str:
    from codey.codemode.json_edit import edit_guidance

    if not {"edit", "read_file"} <= {entry.name for entry in snapshot.entries}:
        return ""
    edit = next(entry for entry in snapshot.entries if entry.name == "edit")
    parameters = canonical_json_object(edit.parameters)
    return edit_guidance(require_json_strings(parameters.get("required", []), "required"),
                         tuple(canonical_json_object(parameters.get("properties", {}))))


def _entry_signatures(entry: ToolCatalogEntry, *, json_edit: bool) -> tuple[str, ...]:
    value = schema_typescript(entry.output_schema)
    ordinary = f"{entry.name}(args: {schema_typescript(entry.parameters)}): Result<{value}>;"
    if entry.name == "edit" and json_edit:
        return (f"edit(args: {{base_result_ref: string; changes: Record<string, JsonEditValue>}}): Result<{value}>;",
                ordinary)
    return (ordinary,)


def _entry_declarations(entry: ToolCatalogEntry, *, json_edit: bool, include_description: bool = True) -> tuple[str, ...]:
    lines = [f"  // {line}" for line in entry.description.splitlines()] if include_description else []
    for name, schema in canonical_json_object(entry.parameters.get("properties", {})).items():
        description = canonical_json_object(schema).get("description")
        if isinstance(description, str) and description:
            lines.append(f"  // {entry.name}.{name}: {description}")
    lines.extend("  " + signature for signature in _entry_signatures(entry, json_edit=json_edit))
    return tuple(lines)


def render_declarations(snapshot: ToolCatalogSnapshot, *, budget_bytes: int = MODEL_CATALOG_BUDGET_BYTES,
                        outer_descriptions: Mapping[str, str] | None = None) -> str:
    """Render a bounded model view; discovery retains the complete frozen contract."""
    from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE

    guidance = _edit_guidance(snapshot)
    json_edit = guidance == JSON_FIELD_EDIT_GUIDANCE
    header = ["// tools.NAME uses NAME's declared arguments; returns Result<T>."]
    header.extend("// " + line for line in guidance.splitlines())
    if json_edit:
        header.append("type JsonEditValue = null | boolean | number | string | JsonEditValue[] | {[key: string]: JsonEditValue};")
    header.extend((
        "type ToolResult<T> = {ok: true; value: T; result_ref?: string} | {ok: false; error: string};",
        "type Result<T> = Promise<ToolResult<T>>;",
        "declare function unwrap<T>(result: ToolResult<T>): T;",
        "declare const tools: {",
    ))
    footer = [
        "};", "type CatalogSearchItem = {path: string; signature: string; description: string};",
        "declare function search(query?: string, options?: {namespace?: string; limit?: number; offset?: number}): Promise<{items: CatalogSearchItem[]; remaining: number; next_offset: number | null}>;",
        "declare function describe(path: string): Promise<CatalogSearchItem | null>;",
        "// Discovery only: await describe('tools.NAME') for the full omitted declaration. Calls still require authorization.",
        f"// catalog_digest={snapshot.digest}",
    ]
    shared = {entry.name for entry in snapshot.entries
              if outer_descriptions is not None and outer_descriptions.get(entry.name) == entry.description}
    if shared:
        footer.append("// Also callable as tools.NAME with the exact already-visible NAME arguments and Result<T>: "
                      + ", ".join(sorted(shared)) + ". Full signatures remain available through describe('tools.NAME').")
    groups: dict[str, int] = {}
    for entry in snapshot.entries:
        namespace = _catalog_namespace(entry)
        groups[namespace] = groups.get(namespace, 0) + 1
    footer.extend(f"// {namespace}: {count} tools" for namespace, count in sorted(groups.items()))
    size = len("\n".join((*header, *footer)).encode("utf-8"))
    if type(budget_bytes) is not int or budget_bytes < size:
        raise ValueError(f"catalog budget must cover the {size}-byte discovery header")
    selected: dict[str, tuple[str, ...]] = {}
    priority = {name: index for index, name in enumerate(_PINNED)}

    def entry_order(entry: ToolCatalogEntry) -> tuple[int, str]:
        return priority.get(entry.name, len(priority)), entry.name

    for entry in sorted(snapshot.entries, key=entry_order):
        if entry.name in shared:
            continue
        lines = _entry_declarations(entry, json_edit=json_edit,
            include_description=outer_descriptions is None or outer_descriptions.get(entry.name) != entry.description)
        cost = len(("\n".join(lines) + "\n").encode("utf-8"))
        if size + cost <= budget_bytes:
            selected[entry.name] = lines
            size += cost
    body = [line for entry in snapshot.entries for line in selected.get(entry.name, ())]
    return "\n".join((*header, *body, *footer))


def _catalog_namespace(entry: ToolCatalogEntry) -> str:
    return entry.namespace or "tools"


def _catalog_item(entry: ToolCatalogEntry, snapshot: ToolCatalogSnapshot) -> CatalogSearchItem:
    from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE

    signatures = _entry_signatures(entry, json_edit=_edit_guidance(snapshot) == JSON_FIELD_EDIT_GUIDANCE)
    declarations = _entry_declarations(entry, json_edit=_edit_guidance(snapshot) == JSON_FIELD_EDIT_GUIDANCE)
    descriptions = [line.removeprefix("  // ") for line in declarations if line.startswith("  // ")]
    if entry.name == "edit" and _edit_guidance(snapshot):
        descriptions.append(_edit_guidance(snapshot))
    return CatalogSearchItem("tools." + entry.name, "\n".join("tools." + line for line in signatures),
                             "\n".join(descriptions))


def catalog_summary(snapshot: ToolCatalogSnapshot, *, budget: int = 2_000) -> CatalogSummary:
    """Budget in UTF-8 bytes, including namespace counts and short signatures."""
    if type(budget) is not int or budget <= 0:
        raise ValueError("catalog summary budget must be a positive byte count")
    groups: dict[str, list[CatalogSearchItem]] = {}
    for entry in snapshot.entries:
        groups.setdefault(_catalog_namespace(entry), []).append(_catalog_item(entry, snapshot))
    lines = [f"{namespace}: {len(entries)} tools" for namespace, entries in sorted(groups.items())]
    used = len("\n".join(lines).encode("utf-8"))
    if used > budget:
        raise ValueError("catalog summary budget cannot cover namespace counts")
    selected: dict[str, list[CatalogSearchItem]] = {namespace: [] for namespace in groups}
    candidates = [(namespace, item) for namespace, entries in groups.items() for item in entries]
    priority = {"tools." + name: index for index, name in enumerate(_PINNED)}

    def item_order(pair: tuple[str, CatalogSearchItem]) -> tuple[int, str]:
        return priority.get(pair[1].path, len(priority)), pair[1].path

    for namespace, item in sorted(candidates, key=item_order):
        line = "- " + item.signature.splitlines()[0]
        cost = len(("\n" + line).encode("utf-8"))
        if used + cost <= budget:
            lines.append(line)
            selected[namespace].append(item)
            used += cost
    namespaces = tuple(CatalogNamespaceSummary(name, len(items), tuple(selected[name]))
                       for name, items in sorted(groups.items()))
    return CatalogSummary(len(snapshot.entries), sum(len(items) for items in selected.values()), namespaces,
                          "\n".join(lines))


def _search_terms(value: str) -> tuple[str, ...]:
    return tuple(term for term in re.split(r"[\W_]+", value.casefold()) if term)


def catalog_search(
    snapshot: ToolCatalogSnapshot,
    query: str = "",
    *,
    namespace: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> CatalogSearchResult:
    if type(limit) is not int or limit <= 0 or type(offset) is not int or offset < 0:
        raise ValueError("catalog search limit must be positive and offset must be non-negative")
    normalized_namespace = namespace.casefold() if namespace is not None else None
    terms = _search_terms(query)
    ranked: list[tuple[int, CatalogSearchItem]] = []
    for entry in snapshot.entries:
        item = _catalog_item(entry, snapshot)
        current_namespace = _catalog_namespace(entry).casefold()
        if normalized_namespace is not None and current_namespace != normalized_namespace:
            continue
        haystack = " ".join((item.path, item.description, item.signature)).casefold()
        words = _search_terms(haystack)
        if terms and not all(term in words or term in haystack for term in terms):
            continue
        score = sum((3 if term in _search_terms(item.path) else 1) for term in terms)
        ranked.append((-score, item))
    def ranking(pair: tuple[int, CatalogSearchItem]) -> tuple[int, str]:
        return pair[0], pair[1].path

    ranked.sort(key=ranking)
    matches = [item for _, item in ranked]
    page = tuple(matches[offset:offset + limit])
    remaining = max(0, len(matches) - offset - len(page))
    next_offset = offset + len(page) if remaining else None
    return CatalogSearchResult(page, remaining, next_offset)


def catalog_describe(snapshot: ToolCatalogSnapshot, path: str) -> CatalogSearchItem | None:
    return next((_catalog_item(entry, snapshot) for entry in snapshot.entries if "tools." + entry.name == path), None)
