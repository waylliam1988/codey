"""Observed actionability before native input; dispatch outcomes remain separate."""
from __future__ import annotations

from playwright.sync_api import ElementHandle

from codey.types.json_value import JsonObject, decode_json_object

_HIT_TEST = """el => {
    if (!el.isConnected) return {ready: false, reason: 'stale_ref'};
    if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true')
        return {ready: false, reason: 'disabled_control'};
    const box = el.getBoundingClientRect();
    const x = Math.max(0, box.left) + (Math.min(innerWidth, box.right) - Math.max(0, box.left)) / 2;
    const y = Math.max(0, box.top) + (Math.min(innerHeight, box.bottom) - Math.max(0, box.top)) / 2;
    if (box.width <= 0 || box.height <= 0 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight)
        return {ready: false, reason: 'stale_ref'};
    const blocked = hit => ({ready: false, reason: 'occluded',
        blocker: hit ? {tag: hit.tagName.toLowerCase(), role: hit.getAttribute('role') || '',
            name: (hit.getAttribute('aria-label') || hit.innerText || '').slice(0, 240)} : null});
    let target = el, root = el.getRootNode();
    while (root instanceof ShadowRoot) {
        const hit = root.elementFromPoint(x, y);
        if (!hit || hit !== target && !target.contains(hit)) return blocked(hit);
        target = root.host;
        root = target.getRootNode();
    }
    const hit = el.ownerDocument.elementFromPoint(x, y);
    if (!hit || hit !== target && !target.contains(hit)) return blocked(hit);
    return {ready: true};
}"""


def check_actionability(handle: ElementHandle) -> JsonObject:
    """This sends no click. The native click must still handle later page changes."""
    handle.scroll_into_view_if_needed(timeout=2000)
    raw: object = handle.evaluate(_HIT_TEST)
    return decode_json_object(raw)
