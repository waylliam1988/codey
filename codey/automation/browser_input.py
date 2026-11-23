"""Observed actionability before native input; dispatch outcomes remain separate."""
from __future__ import annotations

from playwright.sync_api import ElementHandle

from codey.types.json_value import JsonObject, decode_json_object

_HIT_TEST = """async el => {
    if (!el.isConnected) return {ready: false, reason: 'stale_ref'};
    if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true')
        return {ready: false, reason: 'disabled_control'};
    const first = el.getBoundingClientRect();
    await new Promise(resolve => {
        const timer = setTimeout(resolve, 48);
        requestAnimationFrame(() => { clearTimeout(timer); resolve(); });
    });
    if (!el.isConnected) return {ready: false, reason: 'stale_ref'};
    const box = el.getBoundingClientRect();
    if (['left','top','width','height'].some(key => Math.abs(first[key] - box[key]) > 0.5))
        return {ready: false, reason: 'unstable_control'};
    const left = Math.max(0, box.left), top = Math.max(0, box.top);
    const width = Math.min(innerWidth, box.right) - left, height = Math.min(innerHeight, box.bottom) - top;
    if (width <= 0 || height <= 0)
        return {ready: false, reason: 'stale_ref'};
    const blocked = (hit, x, y) => ({ready: false, reason: 'occluded', hit_point: {x, y},
        blocker: hit ? {tag: hit.tagName.toLowerCase(), role: hit.getAttribute('role') || '',
            name: (hit.getAttribute('aria-label') || hit.innerText || '').slice(0, 240)} : null});
    function blocker(x, y) {
        let target = el, root = el.getRootNode();
        while (root instanceof ShadowRoot) {
            const hit = root.elementFromPoint(x, y);
            if (!hit || hit !== target && !target.contains(hit)) return blocked(hit, x, y);
            target = root.host;
            root = target.getRootNode();
        }
        const hit = el.ownerDocument.elementFromPoint(x, y);
        return !hit || hit !== target && !target.contains(hit) ? blocked(hit, x, y) : null;
    }
    let firstBlocker = null;
    for (const [h,v] of [[.5,.5],[.25,.25],[.75,.25],[.25,.75],[.75,.75]]) {
        const x = left + width*h, y = top + height*v;
        const hit = blocker(x, y);
        if (!hit) return {ready: true, position: {x: x - box.left - el.clientLeft, y: y - box.top - el.clientTop}};
        firstBlocker ??= hit;
    }
    return firstBlocker;
}"""


def check_actionability(handle: ElementHandle) -> JsonObject:
    """This sends no click. The native click must still handle later page changes."""
    handle.scroll_into_view_if_needed(timeout=2000)
    raw: object = handle.evaluate(_HIT_TEST)
    return decode_json_object(raw)
