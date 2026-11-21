"""Read explicit DOM control relationships through the existing Chromium session."""
from __future__ import annotations

import json
from contextlib import suppress
from uuid import uuid4

from playwright.sync_api import CDPSession, ElementHandle, Error, Page

from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string

QUERY_TARGET = """el => {
    let target = null, relation = '';
    if (el.matches('summary') && el.parentElement?.matches('details')) {
        target = el.parentElement; relation = 'native-details';
    } else {
        const ids = (el.getAttribute('aria-controls') || '').trim().split(/\\s+/).filter(Boolean);
        if (ids.length !== 1) return {relation, node: null};
        const matches = el.getRootNode().querySelectorAll('#' + CSS.escape(ids[0]));
        if (matches.length !== 1) return {relation, node: null};
        target = matches[0]; relation = 'aria-controls';
    }
    return {relation, node: target};
}"""
_STATE = """function() {
    if (!this.isConnected || this.ownerDocument !== document) return null;
    return {text: (this.innerText || '').replace(/\\s+/g, ' ').trim(),
            visible: !!this.getClientRects().length && getComputedStyle(this).visibility === 'visible',
            busy: !!this.closest('[aria-busy=true]') || !!this.querySelector('[aria-busy=true]')};
}"""


class QueryObservationError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def changed_query_region(before: JsonObject, after: JsonObject) -> str:
    old, new = before.get("regions"), after.get("regions")
    if not isinstance(old, list) or len(old) != 1 or not isinstance(new, list) or len(new) != 1:
        return ""
    previous, current = old[0], new[0]
    if (isinstance(previous, dict) and isinstance(current, dict) and previous.get("key") == current.get("key")
            and current.get("busy") is False and isinstance(body := current.get("text"), str)
            and current.get("visible") is True
            and (body != previous.get("text") or previous.get("visible") is False)):
        return body
    return ""


def _send(cdp: CDPSession, method: str, params: dict[str, object] | None = None) -> JsonObject:
    raw: object = cdp.send(method, params)
    return decode_json_object(raw)


def bind_query_target(page: Page, control: ElementHandle) -> JsonObject | None:
    """Persist a browser node identity, never a selector to rediscover after restart."""
    observed = control.evaluate_handle(QUERY_TARGET)
    try:
        node_handle = observed.get_property('node')
        target = node_handle.as_element()
        if target is None:
            node_handle.dispose()
            return None
        relation_handle = observed.get_property('relation')
        try:
            raw_relation: object = relation_handle.json_value()
            relation = require_json_string(raw_relation, 'relation')
        finally:
            relation_handle.dispose()
        try:
            backend = _backend_node(page, target)
            binding: JsonObject = {'backend_node_id': backend, 'relation': relation}
            observe_query_target(page, binding)
            still_controlled: object = control.evaluate(
                '(el, node) => { const current = (' + QUERY_TARGET + ')(el); return !!current && current.node === node; }', target)
            if still_controlled is not True:
                raise QueryObservationError('query_target_changed', 'The control relationship changed before the click.')
            return binding
        finally:
            target.dispose()
    except Error as exc:
        raise QueryObservationError('query_target_unavailable', 'The controlled node is unavailable before the click.') from exc
    finally:
        observed.dispose()


def _backend_node(page: Page, node: ElementHandle) -> int:
    """Bridge an actual handle to CDP; no selector, index or persistent node map."""
    cdp = page.context.new_cdp_session(page)
    key = '__codey_query_node_' + uuid4().hex
    try:
        # Playwright and CDP do not share public handle IDs. This temporary,
        # nonenumerable reference crosses that boundary and is consumed at once.
        args: dict[str, object] = {'node': node, 'key': key}
        _value: object = page.evaluate('''({node, key}) => {
            Object.defineProperty(globalThis, key, {value: node, configurable: true});
        }''', args)
        remote = _send(cdp, 'Runtime.evaluate', {
            'expression': '(() => { const node = globalThis[' + json.dumps(key)
                + ']; delete globalThis[' + json.dumps(key) + ']; return node; })()',
            'objectGroup': 'codey-query-binding',
        })
        handle = decode_json_object(remote['result'])
        described = _send(cdp, 'DOM.describeNode', {'objectId': require_json_string(handle['objectId'], 'node object')})
        return require_json_integer(decode_json_object(described['node'])['backendNodeId'], 'backend node')
    finally:
        with suppress(Error):
            _value = page.evaluate('key => { delete globalThis[key]; }', key)
        _send(cdp, 'Runtime.releaseObjectGroup', {'objectGroup': 'codey-query-binding'})
        cdp.detach()


def observe_query_target(page: Page, binding: JsonObject) -> JsonObject:
    cdp = page.context.new_cdp_session(page)
    try:
        resolved = _send(cdp, 'DOM.resolveNode', {
            'backendNodeId': require_json_integer(binding['backend_node_id'], 'backend node'),
            'objectGroup': 'codey-query-observation',
        })
        remote = decode_json_object(resolved['object'])
        result = _send(cdp, 'Runtime.callFunctionOn', {
            'objectId': require_json_string(remote['objectId'], 'object id'),
            'functionDeclaration': _STATE, 'returnByValue': True,
        })
        value = decode_json_object(result['result']).get('value')
        if not isinstance(value, dict):
            raise QueryObservationError('query_target_detached', 'The original query node is no longer connected.')
        state = decode_json_object(value)
        return {'key': str(binding['backend_node_id']), **state}
    except Error as exc:
        raise QueryObservationError('query_target_unavailable', 'The original query node is unavailable.') from exc
    finally:
        _send(cdp, 'Runtime.releaseObjectGroup', {'objectGroup': 'codey-query-observation'})
        cdp.detach()
