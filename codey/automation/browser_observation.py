"""Observed structural labels and their text projection; no business ownership inference."""
from __future__ import annotations

from playwright.sync_api import ElementHandle

from codey.types.json_value import JsonObject, decode_json_object

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
        rows.append(("  " if context is not None else "")
                    + f"[{element['ref']}] {element['role']} \"{element['name']}\""
                    + (f" value={element['value']!r}" if element['value'] is not None else "")
                    + (" disabled" if element['disabled'] else ""))
    return "\n".join(rows)
