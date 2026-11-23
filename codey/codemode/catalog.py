from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.codemode.navigation import TASK_SOURCE_TOOLS, task_navigation_schema, task_source_schema
from codey.codemode.patch import MAX_PATCH_FILES, PATCH_DESCRIPTION
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
from codey.toolchain.tool_spec import ToolSpec, _schema_for_spec, is_text_batch_tool
from codey.types.json_value import JsonObject, canonical_json_object, require_json_strings

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
        task_source_url=snapshot.bound_source_url,
        task_source_selector=snapshot.input_contract_version >= 13 and snapshot.bound_source_url is not None,
        browser_followup_checks=(snapshot.policy.required_checks if snapshot.policy else ())
        if snapshot.browser_followups else None,
        observe_link_navigation=(snapshot.input_contract_version >= 21
                                 and snapshot.research_scope is None),
        browser_result_reads=snapshot.research_scope is not None,
        browser_references=snapshot.browser_references if observed_choices else None,
        browser_flows=snapshot.input_contract_version >= 16 and snapshot.browser_followups,
        semantic_edits=snapshot.input_contract_version >= 17,
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
            "parameters": _schema_for_spec(spec),
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
    task_source_selector: bool = True,
    task_source_url: str | None = None,
    browser_followup_checks: tuple[str, ...] | None = None,
    observe_link_navigation: bool = False,
    browser_result_reads: bool = False,
    browser_references: BrowserReferences | None = None,
    browser_flows: bool = False,
    semantic_edits: bool = False,
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
        control_steps = plans.get("controls")
        if isinstance(control_steps, list) and any(spec.name == "browser_read" and not spec.required for spec in values):
            plans["controls"] = [*control_steps, {"name": "browser_read", "args": JsonObject()}]
        from codey.operations.browser_followups import add_link_observation

        add_link_observation(plans, browser_followup_checks, enabled=observe_link_navigation)
        purposes = tuple(purpose for purpose in PURPOSES if purpose in plans)
        values = tuple(model_spec(spec, None, click_purposes=purposes)
                       if spec.name == "browser_click" else spec for spec in values)
    result_actions = {spec.name for spec in values if spec.executor == "browser" and spec.effect == "page_mutation"}
    snapshot = build_catalog_from_specs(values)
    allowed: list[ToolCatalogEntry] = []
    for entry in snapshot.entries:
        if is_text_batch_tool(entry.name):
            continue
        if (
            (entry.read_only and entry.namespace in {"project", "source", "knowledge"}
             and entry.grant in {"project.read", "web.read", "knowledge.read"})
            or entry.namespace in {"browser", "task_control"}
            or entry.namespace == "receipt" and entry.name in {"read_data", "calculate"} and entry.grant == "control"
            or (
                entry.namespace == "project"
                and ((entry.grant == "project.write" and allow_project_edit and entry.name == "edit")
                     or (entry.grant == "project.verify" and allow_project_run and entry.name == "run"))
            )
        ):
            if entry.name == "edit":
                parameters = canonical_json_object(entry.parameters)
                properties = canonical_json_object(parameters.get("properties", {}))
                if {"replacements", "expected_version"} <= properties.keys():
                    patch_fields: JsonObject = {key: properties[key] for key in ("path", "base_result_ref", "replacements")}
                    entry = replace(entry, parameters={**parameters, "x-codey-patch": {
                        "type": "object", "properties": {"files": {"type": "array", "minItems": 2,
                            "maxItems": MAX_PATCH_FILES, "items": {"type": "object", "properties": patch_fields,
                                "required": ["path", "replacements"], "additionalProperties": False}}},
                        "required": ["files"], "additionalProperties": False}},
                        description=entry.description + " " + PATCH_DESCRIPTION)
            if entry.name == "browser_navigate" and task_navigation_url is not None:
                entry = replace(entry, description='Use {target: "task"} to open the task URL at the chosen time. '
                    'Use {url: "..."} for other authorized pages. ' + entry.description,
                    parameters={"anyOf": [
                        task_navigation_schema(),
                        canonical_json_object(entry.parameters),
                    ]})
            if entry.name in TASK_SOURCE_TOOLS and task_source_selector and (task_source_url or task_navigation_url):
                original = canonical_json_object(entry.parameters)
                entry = replace(entry, description='Select target="task" to read the original task URL without copying it. '
                    'Use url for another authorized source. ' + entry.description,
                    parameters={"anyOf": [task_source_schema(original), original]})
            if browser_result_reads and entry.name in result_actions and any(
                    spec.name == "browser_read" and not spec.required for spec in values):
                entry = replace(entry, parameters={**canonical_json_object(entry.parameters),
                    "x-codey-result-read": True}, description=entry.description +
                    " In whole-answer Research, a delivered page action also acquires current formal page text "
                    "after its follow-ups. The action receipt remains authoritative if acquisition fails.")
            allowed.append(entry)
    if browser_followup_checks is not None:
        for index, entry in enumerate(allowed):
            if entry.name == "browser_click":
                allowed[index] = replace(entry, parameters={**canonical_json_object(entry.parameters),
                                                           "x-codey-click-plans": plans})
    if browser_flows and any(entry.name == "browser_read" for entry in allowed):
        from codey.codemode.browser_flow import flow_schema

        entries = {entry.name: entry for entry in allowed}
        for index, entry in enumerate(allowed):
            if entry.name == "browser_snapshot":
                allowed[index] = replace(entry, parameters={**canonical_json_object(entry.parameters),
                    "x-codey-browser-flow": flow_schema(entries)}, description=entry.description +
                    " Use browser_snapshot({flow: [{tool, args}, ...]}) for ordered browser observations. "
                    "The flow stops at the first rejected step and returns the observed results in order.")
    if semantic_edits and any(entry.name == "semantic_definition" for entry in allowed):
        allowed = _semantic_edit_catalog(allowed)
    return build_catalog_snapshot(allowed)


def _semantic_edit_catalog(entries: list[ToolCatalogEntry]) -> list[ToolCatalogEntry]:
    contract: JsonObject = {"type": "object", "properties": {"semantic_result_ref": {"type": "string"}},
                           "required": ["semantic_result_ref"], "additionalProperties": False}
    return [replace(entry, parameters={**canonical_json_object(entry.parameters), "x-codey-semantic-edit": contract})
            if entry.name == "edit" and "x-codey-patch" in entry.parameters else entry for entry in entries]


_PINNED = ("read_file", "grep", "browser_snapshot", "edit")
MODEL_CATALOG_BUDGET_BYTES = 24_000
_PATCH_RESULT_TYPE = (
    'type PatchFileResult = {path: string; state: "changed" | "unchanged" | "failed" | "not_dispatched" | "unknown"; result_ref?: string};'
)
_JSON_EDIT_TYPE = "type JsonEditValue = null | boolean | number | string | JsonEditValue[] | {[key: string]: JsonEditValue};"
_NESTED_SCHEMA_METADATA = frozenset({
    "x-codey-patch", "x-codey-semantic-edit", "x-codey-browser-flow", "x-codey-click-plans", "x-codey-result-read",
})


def _edit_guidance(snapshot: ToolCatalogSnapshot) -> str:
    from codey.codemode.json_edit import edit_guidance

    if not {"edit", "read_file"} <= {entry.name for entry in snapshot.entries}:
        return ""
    edit = next(entry for entry in snapshot.entries if entry.name == "edit")
    parameters = canonical_json_object(edit.parameters)
    return edit_guidance(require_json_strings(parameters.get("required", []), "required"),
                         tuple(canonical_json_object(parameters.get("properties", {}))))


def _entry_signatures(entry: ToolCatalogEntry, *, json_edit: bool, include_ordinary: bool = True,
                      compact: bool = False) -> tuple[str, ...]:
    value = schema_typescript(entry.output_schema)
    signatures = ([f"{entry.name}(args: {schema_typescript(entry.parameters)}): Result<{value}>;"]
                  if include_ordinary else [])
    if entry.name == "edit" and json_edit:
        signatures.append(f"edit(args: {{base_result_ref: string; changes: Record<string, JsonEditValue>}}): Result<{value}>;")
    patch = canonical_json_object(entry.parameters).get("x-codey-patch")
    if isinstance(patch, dict):
        result = (f"Promise<ToolResult<{value}> & {{patch?: PatchFileResult[]}}>" if compact else f"Result<{value}>")
        signatures.append(f"edit(args: {schema_typescript(patch)}): {result};")
    semantic = canonical_json_object(entry.parameters).get("x-codey-semantic-edit")
    if isinstance(semantic, dict):
        signatures.append(f"edit(args: {schema_typescript(semantic)}): Result<{value}>;")
    flow = canonical_json_object(entry.parameters).get("x-codey-browser-flow")
    if isinstance(flow, dict):
        signatures.append(f"browser_snapshot(args: {schema_typescript(flow)}): Result<{{steps: Array<{{tool: string; result_ref: string; value: unknown}}>}}>;")
    return tuple(signatures)


def _entry_comments(entry: ToolCatalogEntry, *, include_description: bool = True) -> list[str]:
    lines = entry.description.splitlines() if include_description else []
    for name, schema in canonical_json_object(entry.parameters.get("properties", {})).items():
        description = canonical_json_object(schema).get("description")
        if isinstance(description, str) and description:
            lines.append(f"{entry.name}.{name}: {description}")
    return lines


def _entry_declarations(entry: ToolCatalogEntry, *, json_edit: bool, include_description: bool = True) -> tuple[str, ...]:
    return (*(f"  // {line}" for line in _entry_comments(entry, include_description=include_description)),
            *("  " + signature for signature in _entry_signatures(entry, json_edit=json_edit)))


def _entry_type_declarations(entry: ToolCatalogEntry, *, json_edit: bool) -> tuple[str, ...]:
    types: list[str] = []
    if "x-codey-patch" in entry.parameters:
        types.append(_PATCH_RESULT_TYPE)
    if entry.name == "edit" and json_edit:
        types.append(_JSON_EDIT_TYPE)
    return tuple(types)


def _compact_entry(entry: ToolCatalogEntry, outer: ToolSpec | None, *, guidance: str,
                   json_edit: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ordinary = {key: value for key, value in entry.parameters.items() if key not in _NESTED_SCHEMA_METADATA}
    same_input = outer is not None and canonical_json_object(ordinary) == _schema_for_spec(outer)
    signatures = _entry_signatures(entry, json_edit=json_edit, include_ordinary=not same_input, compact=True)
    if not signatures:
        return (), ()
    comments = [] if same_input else [entry.description]
    if same_input and "x-codey-patch" in entry.parameters:
        comments.append(PATCH_DESCRIPTION)
    if entry.name == "edit" and json_edit:
        comments.extend(guidance.splitlines())
    lines = ["  // " + line for comment in comments for line in comment.splitlines()]
    return _entry_type_declarations(entry, json_edit=json_edit), (*lines, *("  " + signature for signature in signatures))


def render_declarations(snapshot: ToolCatalogSnapshot, *, budget_bytes: int = MODEL_CATALOG_BUDGET_BYTES,
                        outer_descriptions: Mapping[str, str] | None = None,
                        outer_specs: Iterable[ToolSpec] = (), detail_budget_bytes: int | None = None,
                        detail_names: tuple[str, ...] = (), include_json_edit: bool = False) -> str:
    """Render a bounded model view; discovery retains the complete frozen contract."""
    from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE

    guidance = _edit_guidance(snapshot)
    json_edit = guidance == JSON_FIELD_EDIT_GUIDANCE
    compact = detail_budget_bytes is not None
    header = ["// tools.NAME uses NAME's declared arguments; returns Result<T>."]
    if compact:
        header.append("// Match tools by name with describe('tools.NAME'); search() also returns signatures.")
    else:
        header.extend("// " + line for line in guidance.splitlines())
        if json_edit:
            header.append(_JSON_EDIT_TYPE)
        header.append(_PATCH_RESULT_TYPE)
    patch_result = "" if compact else " & {patch?: PatchFileResult[]}"
    header.extend((
        "type ToolResult<T> = ({ok: true; value: T; result_ref?: string} | {ok: false; error: string})" + patch_result + ";",
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
    shared = {entry.name for entry in snapshot.entries if not compact
              and outer_descriptions is not None and outer_descriptions.get(entry.name) == entry.description}
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
    if detail_budget_bytes is not None and detail_budget_bytes < 0:
        raise ValueError("catalog detail budget must be non-negative")
    remaining = budget_bytes - size
    if detail_budget_bytes is not None:
        remaining = min(remaining, detail_budget_bytes)
    selected: dict[str, tuple[str, ...]] = {}
    types: list[str] = []
    outer = {spec.name: spec for spec in outer_specs}
    priority = {name: index for index, name in enumerate(detail_names if compact else _PINNED)}

    def entry_order(entry: ToolCatalogEntry) -> tuple[int, str]:
        return priority.get(entry.name, len(priority)), entry.name

    for entry in sorted(snapshot.entries, key=entry_order):
        if entry.name in shared or compact and entry.name not in priority:
            continue
        if compact:
            dependencies, lines = _compact_entry(entry, outer.get(entry.name), guidance=guidance,
                                                 json_edit=json_edit and include_json_edit)
        else:
            dependencies = ()
            lines = _entry_declarations(entry, json_edit=json_edit,
                include_description=outer_descriptions is None or outer_descriptions.get(entry.name) != entry.description)
        new_types = tuple(dependency for dependency in dependencies if dependency not in types)
        cost = sum(len(line.encode("utf-8")) + 1 for line in (*new_types, *lines))
        if cost <= remaining:
            selected[entry.name] = lines
            types.extend(new_types)
            remaining -= cost
    body = [line for entry in snapshot.entries for line in selected.get(entry.name, ())]
    return "\n".join((*header[:-1], *types, header[-1], *body, *footer))


def _catalog_namespace(entry: ToolCatalogEntry) -> str:
    return entry.namespace or "tools"


def _catalog_item(entry: ToolCatalogEntry, snapshot: ToolCatalogSnapshot) -> CatalogSearchItem:
    from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE

    guidance = _edit_guidance(snapshot) if entry.name == "edit" else ""
    json_edit = guidance == JSON_FIELD_EDIT_GUIDANCE
    signatures = _entry_signatures(entry, json_edit=json_edit, compact=True)
    types = _entry_type_declarations(entry, json_edit=json_edit)
    descriptions = _entry_comments(entry)
    if guidance:
        descriptions.append(guidance)
    signature = "\n".join((*(f"tools.{line}" for line in signatures), *types))
    return CatalogSearchItem("tools." + entry.name, signature, "\n".join(descriptions))


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
