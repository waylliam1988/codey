"""Read native HTML disclosures without dispatching an event to the website."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.sync_api import Error, Page

from codey.automation.browser_session import _browser_call
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession


_DETAILS = r"""({task, selected}) => {
    const owners = 'article, section, tr, [role=row], [role=region]';
    const clean = value => (value || '').replace(/\s+/g, ' ').trim();
    const fold = value => clean(value).toLowerCase();
    const taskText = fold(task);
    const keys = text => new Set(text.match(/[a-z0-9]+(?:[-_][a-z0-9]+)*/g) || []);
    const taskKeys = keys(taskText);
    const containsName = name => {
        let at = taskText.indexOf(name);
        while (at !== -1) {
            const end = at + name.length;
            if (!(/[a-z0-9_-]/.test(name[0]) && /[a-z0-9_-]/.test(taskText[at-1] || ''))
                && !(/[a-z0-9_-]/.test(name.at(-1)) && /[a-z0-9_-]/.test(taskText[end] || ''))) return true;
            at = taskText.indexOf(name, at + 1);
        }
        return false;
    };
    const labelOf = node => clean(node.getAttribute('aria-label') ||
        node.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > th[scope=row]')?.innerText);
    const namesRecord = label => {
        const name = fold(label);
        return name.length >= 3 && (containsName(name) ||
            [...keys(name)].some(key => key.length >= 3 && /[a-z]/.test(key) && /\d/.test(key) && taskKeys.has(key)));
    };
    const visible = node => node.getClientRects().length > 0 &&
        getComputedStyle(node).visibility === 'visible' && !node.closest('[hidden], [inert], [aria-hidden=true]');
    const candidates = document.querySelectorAll(owners);
    if (candidates.length > 240) return null;
    const matches = [...candidates].filter(node => visible(node) && namesRecord(labelOf(node)));
    if (matches.length !== 1) return null;
    const owner = matches[0];
    const label = labelOf(owner);
    if (label.length > 160) return null;
    const controls = owner.querySelectorAll('details');
    if (controls.length > 24) return null;

    const textOf = (root, expanded, omitSummary = false) => {
        let visited = 0, length = 0;
        const parts = [];
        const add = text => { parts.push(text); length += text.length; };
        const visit = node => {
            if (++visited > 750 || length > 5500) return;
            if (node.nodeType === Node.TEXT_NODE) { add(node.nodeValue || ''); return; }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            if (node !== root && node.matches(owners) && labelOf(node)) return;
            if (node.matches('script, style, template, noscript, svg, iframe, form, input, textarea, select,' +
                             ' [hidden], [inert], [aria-hidden=true], [aria-busy=true]')) return;
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible' || style.opacity === '0'
                || style.contentVisibility === 'hidden') return;
            if (node.matches('br')) { add('\n'); return; }
            const block = !style.display.startsWith('inline');
            if (block) add('\n');
            if (node.matches('details:not([open])') && node !== expanded) {
                const summary = node.querySelector(':scope > summary');
                if (summary) visit(summary);
            } else {
                for (const child of node.childNodes) {
                    if (visited > 750 || length > 5500) break;
                    if (omitSummary && child === root.firstElementChild && child.nodeName === 'SUMMARY') continue;
                    visit(child);
                }
            }
            if (block) add('\n');
        };
        visit(root);
        return visited > 750 || length > 5500 ? '' :
            parts.join('').split('\n').map(clean).filter(Boolean).join('\n');
    };

    const found = [];
    let chosen = null;
    for (const [index, details] of [...controls].entries()) {
        const summary = details.firstElementChild;
        if (details.open || details.closest(owners) !== owner || !summary?.matches('summary') || !visible(summary)
            || summary.matches('[aria-disabled=true]') || summary.querySelector('a, button, input, [role=button]')
            || details.closest('[aria-busy=true]')) continue;
        const name = clean(summary.innerText);
        if (!name || name.length > 120 || !textOf(details, details, true)) continue;
        found.push({index, name, owner: label});
        if (selected && selected.index === index && selected.name === name && selected.owner === label) {
            const text = textOf(owner, details);
            if (text) chosen = {text: text.startsWith(label) ? text : label + '\n' + text, title: document.title};
        }
    }
    const unique = found.filter(row => found.filter(other => other.name === row.name).length === 1);
    if (unique.length > 6) return null;
    return selected === null ? {controls: unique} : unique.some(row => row.index === selected.index) ? chosen : null;
}"""


@dataclass(frozen=True)
class DetailControl:
    index: int
    name: str
    owner: str

    def payload(self) -> JsonObject:
        return {"index": self.index, "name": self.name, "owner": self.owner}

    def choice(self, document: JsonObject) -> str:
        raw = json.dumps([document, self.payload()], sort_keys=True, ensure_ascii=False)
        return "detail-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _inspect(page: Page, task: str, selected: DetailControl | None = None) -> JsonObject | None:
    # The isolated world shares the DOM with the page, while DOM methods
    # come from Chromium rather than prototypes overridden by site code.
    cdp = page.context.new_cdp_session(page)
    try:
        tree = decode_json_object(cdp.send("Page.getFrameTree"))
        frame = decode_json_object(decode_json_object(tree["frameTree"])["frame"])
        world = decode_json_object(cdp.send("Page.createIsolatedWorld", {
            "frameId": require_json_string(frame["id"], "frame id"), "worldName": "codey-read-disclosure",
        }))
        args = {"task": re.sub(r"https?://[^\s<>，。；]+", "", task), "selected": selected.payload() if selected else None}
        evaluated = decode_json_object(cdp.send("Runtime.evaluate", {
            "contextId": require_json_integer(world["executionContextId"], "context id"),
            "expression": f"({_DETAILS})({json.dumps(args, ensure_ascii=True)})", "returnByValue": True,
        }))
        value = decode_json_object(evaluated["result"]).get("value")
        return decode_json_object(value) if isinstance(value, dict) else None
    finally:
        cdp.detach()


def observe_details(browser: BrowserSession, document: JsonObject, task: str) -> tuple[DetailControl, ...]:
    def observe() -> tuple[DetailControl, ...]:
        if browser._document_identity_on_browser_thread() != document:
            return ()
        row = _inspect(browser._page_on_browser_thread(), task)
        controls = row.get("controls") if row is not None else None
        if not isinstance(controls, list) or browser._document_identity_on_browser_thread() != document:
            return ()
        return tuple(DetailControl(require_json_integer(item["index"], "index"),
                                   require_json_string(item["name"], "name"),
                                   require_json_string(item["owner"], "owner"))
                     for raw in controls for item in (decode_json_object(raw),))

    try:
        return _browser_call(observe)
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except (Error, KeyError, RuntimeError, ValueError):
        return ()


def read_detail(browser: BrowserSession, document: JsonObject, task: str, choice: str) -> dict[str, object] | None:
    def read() -> dict[str, object] | None:
        selected = next((control for control in observe_details(browser, document, task)
                         if control.choice(document) == choice), None)
        if selected is None:
            return None
        value = _inspect(browser._page_on_browser_thread(), task, selected)
        if value is None or browser._document_identity_on_browser_thread() != document:
            return None
        return {"status": "ok", "url": document["url"], "title": value["title"], "text": value["text"],
                "truncated": False, "observation": "current_page"}

    try:
        return _browser_call(read)
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except (Error, KeyError, RuntimeError, ValueError):
        return None
