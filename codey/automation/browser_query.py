"""Read explicit DOM control relationships through the existing Chromium session."""
from __future__ import annotations

from playwright.sync_api import CDPSession, ElementHandle, Error, Page

from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string

_TARGET = """el => {
    let target = null, relation = '', selector = '', index = 0;
    if (el.matches('summary') && el.parentElement?.matches('details')) {
        target = el.parentElement; relation = 'native-details'; selector = 'details';
        index = [...document.querySelectorAll(selector)].indexOf(target);
    } else {
        const ids = (el.getAttribute('aria-controls') || '').trim().split(/\\s+/).filter(Boolean);
        if (ids.length !== 1) return null;
        selector = '#' + CSS.escape(ids[0]);
        const matches = document.querySelectorAll(selector);
        if (matches.length !== 1) return null;
        target = matches[0]; relation = 'aria-controls';
    }
    return {relation, selector, index, text: target.innerText.replace(/\\s+/g, ' ').trim()};
}"""
_STATE = """function() {
    if (!this.isConnected || this.ownerDocument !== document) return null;
    return {text: (this.innerText || '').replace(/\\s+/g, ' ').trim(),
            busy: !!this.closest('[aria-busy=true]') || !!this.querySelector('[aria-busy=true]')};
}"""


class QueryObservationError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _send(cdp: CDPSession, method: str, params: dict[str, object] | None = None) -> JsonObject:
    raw: object = cdp.send(method, params)
    return decode_json_object(raw)


def bind_query_target(page: Page, control: ElementHandle) -> JsonObject | None:
    """Persist a browser node identity, never a selector to rediscover after restart."""
    raw: object = control.evaluate(_TARGET)
    if raw is None:
        return None
    target = decode_json_object(raw)
    cdp = page.context.new_cdp_session(page)
    try:
        root = _send(cdp, 'DOM.getDocument')
        node = decode_json_object(root['root'])
        matches = _send(cdp, 'DOM.querySelectorAll', {
            'nodeId': require_json_integer(node['nodeId'], 'root node'),
            'selector': require_json_string(target['selector'], 'selector'),
        })
        nodes = matches.get('nodeIds')
        index = require_json_integer(target['index'], 'index')
        if not isinstance(nodes, list) or not 0 <= index < len(nodes):
            raise QueryObservationError('query_target_changed', 'The controlled node changed before the click.')
        described = _send(cdp, 'DOM.describeNode', {'nodeId': require_json_integer(nodes[index], 'node')})
        backend = require_json_integer(decode_json_object(described['node'])['backendNodeId'], 'backend node')
    finally:
        cdp.detach()
    # The selector is used only at observation time. Later reads resolve the
    # original browser node, and reject replacements with identical labels/IDs.
    binding: JsonObject = {'backend_node_id': backend, 'relation': target['relation']}
    state = observe_query_target(page, binding)
    again: object = control.evaluate(_TARGET)
    if again != raw or state['text'] != target['text']:
        raise QueryObservationError('query_target_changed', 'The control relationship changed before the click.')
    return binding


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
