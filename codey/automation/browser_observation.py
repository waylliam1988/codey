"""Observed structural labels and their text projection; no business ownership inference."""
from __future__ import annotations

import json

from playwright.sync_api import ElementHandle, Page

from codey.types.json_value import JsonObject, decode_json_object, require_json_string

ELEMENT_METADATA = """el => {
    const owners = 'article,section,tr,[role=row],[role=region],fieldset';
    const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
    let context = null, contextNode = null;
    for (let node = el.parentElement; node; node = node.parentElement) {
        if (!node.matches(owners)) continue;
        const named = clean((node.getAttribute('aria-labelledby') || '').split(/\\s+/)
            .map(id => node.getRootNode().getElementById(id)?.innerText || '').join(' '));
        const heading = node.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,' +
            ':scope > th[scope=row],:scope > legend');
        const label = clean(node.getAttribute('aria-label')) || named || clean(heading?.innerText);
        if (!label) continue;
        contextNode = node;
        context = {kind: node.getAttribute('role') || node.tagName.toLowerCase(), label: label.slice(0, 240),
            basis: node.getAttribute('aria-label') ? 'aria-label' : named ? 'aria-labelledby' : 'heading'};
        break;
    }
    return {contextNode, metadata: {
        tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
        name: el.getAttribute('aria-label') || el.getAttribute('title') ||
              Array.from(el.labels || [], label => label.innerText).join(' ') ||
              el.getAttribute('placeholder') || el.innerText || '',
        type: el.getAttribute('type') || '',
        value: el.type === 'password' ? null : ('value' in el ? el.value : null),
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        checked: 'checked' in el ? el.checked : null, context
    }};
}"""

_ELEMENTS_METADATA = """handles => {
    const observe = (""" + ELEMENT_METADATA + """);
    const groups = new WeakMap(), contexts = [], contextNodes = {};
    const elements = handles.map(el => {
        const {metadata, contextNode} = observe(el);
        const {context, ...fields} = metadata;
        let context_id = '';
        if (contextNode) {
            context_id = groups.get(contextNode);
            if (context_id === undefined) {
                context_id = 'c' + contexts.length;
                groups.set(contextNode, context_id);
                contexts.push({id: context_id, ...context});
                contextNodes[context_id] = contextNode;
            }
        }
        return {...fields, context_id};
    });
    return {data: {elements, contexts}, contextNodes};
}"""


def observe_elements(page: Page, handles: list[ElementHandle]) -> tuple[
    list[JsonObject], dict[str, JsonObject], dict[str, ElementHandle],
]:
    """Group actual nodes in one observation; retain handles for execution checks."""
    observed = page.evaluate_handle(_ELEMENTS_METADATA, handles)
    data_handle = observed.get_property('data')
    nodes_handle = observed.get_property('contextNodes')
    try:
        raw: object = data_handle.json_value()
        data = decode_json_object(raw)
        elements, contexts = data['elements'], data['contexts']
        if not isinstance(elements, list) or not isinstance(contexts, list):
            raise ValueError('Invalid observed controls.')
        nodes: dict[str, ElementHandle] = {}
        for key, handle in nodes_handle.get_properties().items():
            node = handle.as_element()
            if node is None:
                raise ValueError('Invalid observed context node.')
            nodes[key] = node
        rows = [decode_json_object(row) for row in contexts]
        return ([decode_json_object(row) for row in elements],
                {require_json_string(row['id'], 'context id'): row for row in rows}, nodes)
    finally:
        data_handle.dispose()
        nodes_handle.dispose()
        observed.dispose()


def observe_element(handle: ElementHandle) -> tuple[JsonObject, ElementHandle | None]:
    """Read metadata and its actual context node in the same DOM observation."""
    observed = handle.evaluate_handle(ELEMENT_METADATA)
    metadata = observed.get_property('metadata')
    try:
        raw: object = metadata.json_value()
        context_node = observed.get_property('contextNode').as_element()
        return decode_json_object(raw), context_node
    finally:
        metadata.dispose()
        observed.dispose()


def same_node(left: ElementHandle, right: ElementHandle) -> bool:
    raw: object = left.evaluate('(node, other) => node === other', right)
    return raw is True


def render_controls(elements: list[JsonObject], contexts: list[JsonObject]) -> str:
    """Render only the recorded relationship, keeping duplicate labels distinct."""
    labels = {row['id']: row for row in contexts if isinstance(row.get('id'), str)}
    previous = ""
    rows: list[str] = []
    for element in elements:
        context_id = element.get('context_id')
        context = labels.get(context_id) if isinstance(context_id, str) else None
        if context is not None and context_id != previous:
            rows.append(f"Context [{context_id}] {context['kind']}: {context['label']} ({context['basis']})")
        previous = context_id if isinstance(context_id, str) else ""
        rows.append(("  " if context is not None else "") + json.dumps(element, ensure_ascii=False))
    return "\n".join(rows)
