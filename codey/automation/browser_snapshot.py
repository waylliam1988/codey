"""Scoped AX/DOM observations and deterministic deltas for the async driver."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping

from playwright.async_api import ElementHandle

from codey.automation.browser_document import same_document
from codey.automation.browser_frames import FrameContext, backend_node, frame_context, resolve_backend
from codey.automation.browser_observation import ACTIONABLE_CANDIDATES, ELEMENT_METADATA, VISIBLE_TEXT
from codey.automation.browser_query_async import bind_query_target
from codey.automation.browser_refs import ElementRecord, ElementRegistry
from codey.types.json_value import JsonObject, JsonValue, decode_json_object

MAX_ELEMENTS = 80
MAX_TEXT = 12_000
AX_CONTROL_ROLES = frozenset({"button", "link", "textbox", "checkbox", "combobox", "menuitem"})


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


async def collect_ax_tree(context: FrameContext) -> dict[int, JsonObject]:
    raw = await context.send("Accessibility.getFullAXTree", {"frameId": context.document["frame_id"]})
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


async def collect_snapshot(context: FrameContext, registry: ElementRegistry[ElementHandle], *,
                           revision: int, max_elements: int = MAX_ELEMENTS, max_text: int = MAX_TEXT,
                           scope_ref: str = "", selector: str = "") -> JsonObject:
    root = await _scope_root(context, registry, scope_ref, selector)
    raw_text: object = await _evaluate(root, VISIBLE_TEXT)
    if not isinstance(raw_text, str):
        raise ValueError("invalid observed text")
    max_elements, max_text = max(1, min(max_elements, MAX_ELEMENTS)), max(1, min(max_text, MAX_TEXT))
    handles = await root.query_selector_all(ACTIONABLE_CANDIDATES)
    root_matches: object = await _evaluate(root, "(el,selector) => el.matches(selector)", ACTIONABLE_CANDIDATES)
    if root_matches is True:
        handles.insert(0, root)
    ax = await collect_ax_tree(context)
    known: set[int] = set()
    rows: list[JsonValue] = []
    contexts: list[JsonValue] = []
    contexts_by_node: dict[int, str] = {}
    elements_truncated = False
    for handle in handles:
        if not await handle.is_visible():
            await handle.dispose()
            continue
        if len(rows) >= max_elements:
            elements_truncated = True
            await handle.dispose()
            continue
        node = await backend_node(context, handle)
        known.add(node)
        metadata, owner = await element_metadata(handle)
        role = str(ax.get(node, {}).get("role") or element_role(metadata))
        name = " ".join(str(ax.get(node, {}).get("name") or metadata.get("name") or "").split())[:240]
        owner_data = metadata.get("context")
        owner_json = decode_json_object(owner_data) if isinstance(owner_data, dict) else None
        context_id = ""
        owner_id = 0
        if owner is not None and owner_json is not None:
            owner_id = await backend_node(context, owner)
            context_id = contexts_by_node.get(owner_id, "")
            if not context_id:
                context_id = f"region-{owner_id}"
                contexts_by_node[owner_id] = context_id
                contexts.append({"id": context_id, **owner_json})
        semantics = semantic_fingerprint(metadata)
        target = await bind_query_target(context, handle) if metadata.get("query_relation") else None
        ref = registry.observe_node(ElementRecord(handle, context.document, node, role, name,
            owner_json, owner, target, revision, semantics, owner_id))
        box_raw: object = await handle.bounding_box()
        row: JsonObject = {"ref": ref, "role": role, "name": name, "context_id": context_id,
                           "disabled": metadata.get("disabled") is True, "value": metadata.get("value"),
                           "checked": metadata.get("checked"), "query_relation": metadata.get("query_relation", "")}
        if isinstance(box_raw, dict):
            row["box"] = decode_json_object(box_raw)
        rows.append(row)
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
    text_truncated = len(raw_text) > max_text
    connected: object = await _evaluate(root, "el => el.isConnected && el.ownerDocument === document")
    async with frame_context(context.frame.page, context.frame, str(context.document["target_id"])) as after:
        if connected is not True or not same_document(context.document, after.document):
            registry.invalidate_document(context.document)
            raise RuntimeError("snapshot_document_changed")
    if not scope_ref and root_matches is not True:
        await root.dispose()
    return {"page_id": context.document["target_id"], "page_url": context.document["url"],
            "generation": revision, "revision": revision, "document": context.document,
            "scope": {"frame_id": context.document["frame_id"], "root_ref": scope_ref, "selector": selector,
                      "max_elements": max_elements, "max_text": max_text},
            "complete": not text_truncated and not elements_truncated, "text_truncated": text_truncated,
            "elements_truncated": elements_truncated, "text": raw_text[:max_text], "elements": rows, "contexts": contexts}


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
