"""Observed structural labels and their text projection; no business ownership inference."""
from __future__ import annotations

import json

from playwright.sync_api import ElementHandle, Page

from codey.automation.browser_query import QUERY_TARGET, _backend_node
from codey.types.json_value import JsonObject, decode_json_object, require_json_string

_CONTEXT = """node => {
    const owners = 'article,section,tr,[role=row],[role=region],fieldset';
    const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
    if (!node.matches(owners)) return null;
    const named = clean((node.getAttribute('aria-labelledby') || '').split(/\\s+/)
        .map(id => node.getRootNode().getElementById(id)?.innerText || '').join(' '));
    const heading = node.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,' +
        ':scope > th[scope=row],:scope > legend');
    const label = clean(node.getAttribute('aria-label')) || named || clean(heading?.innerText);
    return label ? {kind: node.getAttribute('role') || node.tagName.toLowerCase(), label: label.slice(0, 240),
        basis: node.getAttribute('aria-label') ? 'aria-label' : named ? 'aria-labelledby' : 'heading'} : null;
}"""

ELEMENT_METADATA = """el => {
    const query = (""" + QUERY_TARGET + """)(el);
    const observeContext = (""" + _CONTEXT + """);
    let context = null, contextNode = null;
    for (let node = el.parentElement; node; node = node.parentElement) {
        context = observeContext(node);
        if (!context) continue;
        contextNode = node;
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
        checked: 'checked' in el ? el.checked : null, context,
        query_relation: query.node ? query.relation : ''
    }};
}"""

_RECORDS = """labels => {
    const contextOf = (""" + _CONTEXT + """);
    const targetOf = (""" + QUERY_TARGET + """);
    const nodes = document.querySelectorAll('article,section,tr,[role=row],[role=region],fieldset');
    if (nodes.length > 240) return {rows: [], targets: []};
    const rows = [], targets = [], identities = new Map();
    for (const node of labels.includes('') ? [document.body, ...nodes] : nodes) {
        const context = node === document.body ? {label: ''} : contextOf(node);
        if (!context || !labels.includes(context.label) || !node.getClientRects().length ||
            node.closest('[hidden],[inert],[aria-hidden=true],[aria-busy=true]') ||
            getComputedStyle(node).visibility !== 'visible') continue;
        const text = node.innerText;
        if (!text || text.length > 12000) continue;
        const controls = node.querySelectorAll('button,a,[role=button],[role=link]');
        const regions = [];
        if (controls.length <= 240) for (const control of controls) {
            const query = targetOf(control);
            if (!query.node || !node.contains(query.node) || !query.node.getClientRects().length ||
                query.node.closest('[hidden],[inert],[aria-hidden=true],[aria-busy=true]') ||
                getComputedStyle(query.node).visibility !== 'visible') continue;
            const region = query.node.innerText;
            if (!region || region.length > 12000) continue;
            let index = identities.get(query.node);
            if (index === undefined) {
                index = targets.length;
                identities.set(query.node, index);
                targets.push(query.node);
            }
            if (!regions.some(row => row.index === index)) regions.push({index, relation: query.relation, text: region});
        }
        rows.push({context, text, targets: regions});
    }
    return {rows, targets};
}"""


def observe_records(page: Page, labels: tuple[str, ...]) -> list[JsonObject]:
    """Observe named record text; the caller pins only offsets into its source receipt."""
    arguments: list[str] = list(labels)
    observed = page.evaluate_handle(_RECORDS, arguments)
    rows_handle = observed.get_property('rows')
    targets_handle = observed.get_property('targets')
    try:
        raw: object = rows_handle.json_value()
        if not isinstance(raw, list):
            raise ValueError('Invalid observed records.')
        rows = [decode_json_object(row) for row in raw]
        for row in rows:
            regions = row.get('targets')
            bound: list[JsonObject] = []
            for raw_region in regions if isinstance(regions, list) else []:
                region = decode_json_object(raw_region)
                handle = targets_handle.get_property(str(region.pop('index')))
                try:
                    node = handle.as_element()
                    if node is None:
                        raise ValueError('Observed query target is not an element.')
                    identity: JsonObject = {'backend_node_id': _backend_node(page, node), 'relation': region.pop('relation')}
                    region['target'] = identity
                    bound.append(region)
                finally:
                    handle.dispose()
            row['targets'] = list(bound)
        return rows
    finally:
        targets_handle.dispose()
        rows_handle.dispose()
        observed.dispose()

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
