"""Scoped AX/DOM observations and deterministic deltas for the async driver."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

from playwright.async_api import ElementHandle

from codey.automation.browser_document import same_document
from codey.automation.browser_frames import FrameContext, backend_node, backend_nodes, frame_context, resolve_backend
from codey.automation.browser_observation import ACTIONABLE_CANDIDATES, ELEMENT_METADATA, VISIBLE_TEXT
from codey.automation.browser_refs import ElementRecord, ElementRegistry
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer

MAX_ELEMENTS = 80
MAX_TEXT = 12_000
AX_CONTROL_ROLES = frozenset({"button", "link", "textbox", "checkbox", "combobox", "menuitem"})

_DOM_SNAPSHOT = """(root, limit) => {
    const selector = """ + json.dumps(ACTIONABLE_CANDIDATES) + """;
    const observe = (""" + ELEMENT_METADATA + """);
    const visibleText = (""" + VISIBLE_TEXT + """);
    function* descendants(scope) {
        yield* scope.querySelectorAll(selector);
        if (scope.shadowRoot) yield* descendants(scope.shadowRoot);
        for (const node of scope.querySelectorAll('*'))
            if (node.shadowRoot) yield* descendants(node.shadowRoot);
    }
    const nodes = [], rows = [], identities = new Map();
    const index = node => {
        if (!node) return -1;
        if (!identities.has(node)) { identities.set(node, nodes.length); nodes.push(node); }
        return identities.get(node);
    };
    function* candidates() {
        if (root.matches(selector)) yield root;
        yield* descendants(root);
    }
    let truncated = false;
    for (const node of candidates()) {
        const rect = node.getBoundingClientRect();
        if (!rect.width || !rect.height || getComputedStyle(node).visibility !== 'visible') continue;
        const closed = node.closest('details:not([open])');
        if (closed && node !== closed && !closed.querySelector(':scope > summary')?.contains(node)) continue;
        if (rows.length === limit) { truncated = true; break; }
        const {metadata, contextNode, queryNode} = observe(node);
        rows.push({node: index(node), owner: index(contextNode), query: index(queryNode), metadata,
            box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}});
    }
    return {nodes, view: {rows, text: visibleText(root), truncated}};
}"""


def _evaluate(handle: ElementHandle, expression: str, argument: object = None) -> Awaitable[object]:
    evaluate: Callable[[str, object], Awaitable[object]] = handle.evaluate
    return evaluate(expression, argument)


def element_role(metadata: JsonObject) -> str:
    role = str(metadata.get("role") or "").strip().lower()
    if role:
        return role
    tag, kind = str(metadata.get("tag") or ""), str(metadata.get("type") or "")
    if tag == "a":
        return "link"
    if tag in {"button", "summary"}:
        return "button"
    if tag == "select":
        return "combobox"
    if tag in {"input", "textarea"}:
        return {"checkbox": "checkbox", "radio": "radio", "file": "file_input",
                "button": "button", "submit": "button", "reset": "button"}.get(kind, "textbox")
    return "generic"


def semantic_fingerprint(metadata: JsonObject) -> str:
    value: JsonObject = {"identity": metadata.get("semantic_identity"), "role": element_role(metadata),
                         "name": metadata.get("name"), "context": metadata.get("context")}
    return json.dumps(value, sort_keys=True)


async def element_metadata(handle: ElementHandle) -> tuple[JsonObject, ElementHandle | None]:
    observed = await handle.evaluate_handle(ELEMENT_METADATA)
    metadata = await observed.get_property("metadata")
    try:
        read_value: Callable[[], Awaitable[object]] = metadata.json_value
        raw: object = await read_value()
        context = await observed.get_property("contextNode")
        return decode_json_object(raw), context.as_element()
    finally:
        await metadata.dispose()
        await observed.dispose()


async def file_control_details(handle: ElementHandle) -> JsonObject:
    raw: object = await _evaluate(handle, """el => ({
        name:el.getAttribute('aria-label') || Array.from(el.labels||[],label=>label.innerText).join(' ') || el.title,
        dom_id:el.id,field_name:el.name,
        group:el.closest('fieldset')?.querySelector('legend')?.innerText || el.form?.getAttribute('aria-label') || '',
        disabled:el.matches(':disabled') || el.getAttribute('aria-disabled')==='true' || el.type!=='file'
    })""")
    row = decode_json_object(raw)
    return {"disabled": row["disabled"], **{key: " ".join(str(row[key]).split())[:240]
        for key in ("name", "dom_id", "field_name", "group")}}


async def collect_ax_tree(context: FrameContext, *, root_id: int | None = None) -> dict[int, JsonObject]:
    raw = (await context.send("Accessibility.queryAXTree", {"backendNodeId": root_id}) if root_id is not None
           else await context.send("Accessibility.getFullAXTree", {"frameId": context.document["frame_id"]}))
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("invalid accessibility tree")
    result: dict[int, JsonObject] = {}
    for value in nodes:
        node = decode_json_object(value)
        backend = node.get("backendDOMNodeId")
        if type(backend) is int and isinstance(backend, int) and node.get("ignored") is not True:
            properties = node.get("properties", [])
            disabled = any(isinstance(prop, dict) and prop.get("name") == "disabled"
                and decode_json_object(prop.get("value", {})).get("value") is True
                for prop in properties) if isinstance(properties, list) else False
            result[backend] = {"role": decode_json_object(node.get("role", {})).get("value", ""),
                               "name": decode_json_object(node.get("name", {})).get("value", ""), "disabled": disabled}
    return result


async def _scope_root(context: FrameContext, registry: ElementRegistry[ElementHandle],
                      scope_ref: str, selector: str) -> ElementHandle:
    if scope_ref and selector:
        raise ValueError("choose one snapshot scope")
    root: ElementHandle | None
    if scope_ref:
        root = registry.resolve(scope_ref, context.document).handle
    elif selector:
        roots = await context.frame.query_selector_all(selector)
        if len(roots) != 1:
            for handle in roots:
                await handle.dispose()
            raise ValueError("snapshot scope must match exactly one observed region")
        root = roots[0]
    else:
        root = await context.frame.query_selector("body")
    if root is None:
        raise RuntimeError("snapshot scope unavailable")
    return root


async def _dom_snapshot(context: FrameContext, root: ElementHandle, limit: int) -> tuple[
    JsonObject, dict[str, int], dict[str, ElementHandle],
]:
    batch = await root.evaluate_handle(_DOM_SNAPSHOT, limit)
    nodes = await batch.get_property("nodes")
    view = await batch.get_property("view")
    try:
        read_value: Callable[[], Awaitable[object]] = view.json_value
        data = decode_json_object(await read_value())
        identities = await backend_nodes(context, nodes)
        properties = await nodes.get_properties()
        elements = {key: element for key, handle in properties.items() if (element := handle.as_element()) is not None}
        return data, identities, elements
    finally:
        await view.dispose()
        await nodes.dispose()
        await batch.dispose()


async def collect_snapshot(context: FrameContext, registry: ElementRegistry[ElementHandle], *,
                           revision: int, max_elements: int = MAX_ELEMENTS, max_text: int = MAX_TEXT,
                           scope_ref: str = "", selector: str = "") -> JsonObject:
    root = await _scope_root(context, registry, scope_ref, selector)
    max_elements, max_text = max(1, min(max_elements, MAX_ELEMENTS)), max(1, min(max_text, MAX_TEXT))
    dom, identities, handles = await _dom_snapshot(context, root, max_elements)
    raw_text, dom_rows = dom["text"], dom["rows"]
    if not isinstance(raw_text, str) or not isinstance(dom_rows, list):
        raise ValueError("invalid DOM observation")
    root_id = (registry.resolve(scope_ref, context.document).backend_node_id if scope_ref else
               await backend_node(context, root) if selector else None)
    ax = await collect_ax_tree(context, root_id=root_id)
    known: set[int] = set()
    rows: list[JsonValue] = []
    contexts: list[JsonValue] = []
    contexts_by_node: dict[int, str] = {}
    elements_truncated = dom["truncated"] is True
    retained: set[str] = set()
    for raw in dom_rows:
        entry = decode_json_object(raw)
        key = str(require_json_integer(entry["node"], "control index"))
        owner_key = str(require_json_integer(entry["owner"], "owner index"))
        query_key = str(require_json_integer(entry["query"], "query index"))
        handle, node = handles[key], identities[key]
        retained.add(key)
        known.add(node)
        metadata, owner = decode_json_object(entry["metadata"]), handles.get(owner_key)
        role = str(ax.get(node, {}).get("role") or element_role(metadata))
        name = " ".join(str(ax.get(node, {}).get("name") or metadata.get("name") or "").split())[:240]
        owner_data = metadata.get("context")
        owner_json = decode_json_object(owner_data) if isinstance(owner_data, dict) else None
        context_id = ""
        owner_id = 0
        if owner is not None and owner_json is not None:
            retained.add(owner_key)
            owner_id = identities[owner_key]
            context_id = contexts_by_node.get(owner_id, "")
            if not context_id:
                context_id = f"region-{owner_id}"
                contexts_by_node[owner_id] = context_id
                contexts.append({"id": context_id, **owner_json})
        semantics = semantic_fingerprint(metadata)
        target: JsonObject | None = ({"backend_node_id": identities[query_key], "relation": metadata["query_relation"]}
                                    if query_key in identities else None)
        ref = registry.observe_node(ElementRecord(handle, context.document, node, role, name,
            owner_json, owner, target, revision, semantics, owner_id))
        box_raw: object = entry["box"] if context.frame.parent_frame is None else await handle.bounding_box()
        row: JsonObject = {"ref": ref, "role": role, "name": name, "context_id": context_id,
                           "disabled": metadata.get("disabled") is True, "value": metadata.get("value"),
                           "checked": metadata.get("checked"), "query_relation": metadata.get("query_relation", "")}
        if isinstance(box_raw, dict):
            row["box"] = decode_json_object(box_raw)
        rows.append(row)
    for key, handle in handles.items():
        if key not in retained:
            await handle.dispose()
    # AX-only actionable nodes are real DOM identities, not guessed selectors.
    for node, data in ax.items():
        if node in known or data.get("role") not in AX_CONTROL_ROLES:
            continue
        if len(rows) >= max_elements:
            elements_truncated = True
            break
        handle = await resolve_backend(context, node)
        contained: object = await _evaluate(root, "(root,node) => root.contains(node)", handle)
        if contained is not True or not await handle.is_visible():
            await handle.dispose()
            continue
        metadata, owner = await element_metadata(handle)
        role, name = str(data.get("role") or element_role(metadata)), str(data.get("name") or metadata.get("name") or "")
        ref = registry.observe_node(ElementRecord(handle, context.document, node, role, name, None, owner, None,
            revision, semantic_fingerprint(metadata), await backend_node(context, owner) if owner is not None else 0))
        rows.append({"ref": ref, "role": role, "name": name, "context_id": "", "disabled": metadata.get("disabled") is True,
                     "value": metadata.get("value"), "checked": metadata.get("checked"), "query_relation": ""})
    suggestions: list[JsonValue] = await _scope_suggestions(context, registry, root, revision) if elements_truncated else []
    text_truncated = len(raw_text) > max_text
    connected: object = await _evaluate(root, "el => el.isConnected && el.ownerDocument === document")
    async with frame_context(context.frame.page, context.frame, str(context.document["target_id"])) as after:
        if connected is not True or not same_document(context.document, after.document):
            registry.invalidate_document(context.document)
            raise RuntimeError("snapshot_document_changed")
    if not scope_ref:
        await root.dispose()
    return {"page_id": context.document["target_id"], "page_url": context.document["url"],
            "generation": revision, "revision": revision, "document": context.document,
            "scope": {"frame_id": context.document["frame_id"], "root_ref": scope_ref, "selector": selector,
                      "max_elements": max_elements, "max_text": max_text},
            "complete": not text_truncated and not elements_truncated, "text_truncated": text_truncated,
            "elements_truncated": elements_truncated, "text": raw_text[:max_text], "elements": rows, "contexts": contexts,
            **({"scope_suggestions": suggestions} if suggestions else {})}


async def _scope_suggestions(
    context: FrameContext, registry: ElementRegistry[ElementHandle], root: ElementHandle, revision: int,
) -> list[JsonValue]:
    handles = await root.query_selector_all("main,section,article,form,fieldset,nav,[role=region],[role=group]")
    suggestions: list[JsonValue] = []
    for handle in handles:
        if len(suggestions) >= 12 or not await handle.is_visible():
            await handle.dispose()
            continue
        metadata, owner = await element_metadata(handle)
        if owner is not None:
            await owner.dispose()
        label: object = await _evaluate(handle, """el => el.getAttribute('aria-label') ||
            el.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > legend')?.textContent ||
            el.id || el.localName""")
        name = " ".join(str(label or "").split())[:120]
        ref = registry.observe_node(ElementRecord(handle, context.document, await backend_node(context, handle),
            element_role(metadata), name, None, None, None, revision, semantic_fingerprint(metadata), scope_only=True))
        suggestions.append({"ref": ref, "label": name})
    return suggestions


def diff_snapshot(base: Mapping[str, JsonValue] | None, current: JsonObject) -> JsonObject:
    if base is None or any(base.get(key) != current.get(key) for key in ("document", "scope")):
        return {**current, "kind": "full"}
    before_raw, after_raw = base.get("elements"), current.get("elements")
    if not isinstance(before_raw, list) or not isinstance(after_raw, list):
        return {**current, "kind": "full"}
    before = {str(row["ref"]): row for raw in before_raw if isinstance((row := raw), dict)}
    after = {str(row["ref"]): row for raw in after_raw if isinstance((row := raw), dict)}
    added: list[JsonValue] = [row for key, row in after.items() if key not in before]
    changed: list[JsonValue] = [row for key, row in after.items() if key in before and before[key] != row]
    removed: list[JsonValue] = [key for key in before if key not in after]
    result: JsonObject = {key: value for key, value in current.items()
                          if key not in {"elements", "text", "contexts"}}
    for key in ("text", "contexts"):
        if base.get(key) != current.get(key):
            result[key] = current[key]
    return {**result, "kind": "delta", "base_revision": base.get("revision"),
            "added": added, "changed": changed, "removed": removed}
