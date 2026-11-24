"""Observed structural labels and their text projection; no business ownership inference."""
from __future__ import annotations

import json
from collections.abc import Callable

from playwright.sync_api import ElementHandle, JSHandle, Page

from codey.automation.browser_query import QUERY_TARGET, _backend_node
from codey.types.json_value import JsonObject, decode_json_object, require_json_string

ACTIONABLE_CANDIDATES = ("button,summary,a[href],input,textarea,select,[contenteditable=true],"
    "[tabindex],[onclick],[role=button],[role=link],[role=textbox],[role=combobox],"
    "[role=searchbox],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=tab]")

VISIBLE_TEXT = r"""(root, structured = false) => {
    const parts = []; let length = 0;
    const visit = node => {
        if (length > 100000) return;
        if (node.nodeType === Node.TEXT_NODE) {
            const parent = node.parentElement;
            if (!parent || !node.textContent || getComputedStyle(parent).visibility !== 'visible') return;
            const closed = parent.closest('details:not([open])');
            if (closed && !closed.querySelector(':scope > summary')?.contains(parent)) return;
            const range = document.createRange(); range.selectNodeContents(node);
            const boxes = range.getClientRects();
            if (!node.textContent.trim()) {
                if (structured && Array.from(boxes).some(box => box.width > 0)) {parts.push(' '); length++;}
                return;
            }
            if (boxes.length) {
                const text = node.textContent.slice(0,100001-length);
                parts.push(text); length += text.length;
            }
            return;
        }
        if (node.nodeType === Node.ELEMENT_NODE && node.matches('slot')) {
            const assigned = node.assignedNodes({flatten:true});
            if (assigned.length) { assigned.forEach(visit); return; }
        }
        const beginning = parts.length, before = length;
        let prefix = '', suffix = '';
        if (structured && node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName.toLowerCase();
            if (tag === 'br' && node.getClientRects().length) { parts.push('\n'); length++; return; }
            if (/^h[1-6]$/.test(tag)) { prefix = '\n\n' + '#'.repeat(Number(tag[1])) + ' '; suffix = '\n\n'; }
            else if (tag === 'li') { prefix = '\n- '; suffix = '\n'; }
            else if (tag === 'td' || tag === 'th') suffix = '\t';
            else if (tag === 'tr' || tag === 'br') { prefix = '\n'; suffix = '\n'; }
            else if (['p','div','section','article','header','footer','details','summary','ul','ol','table','blockquote','pre'].includes(tag)) {
                prefix = '\n\n'; suffix = '\n\n';
            }
        }
        if (prefix) parts.push(prefix);
        const children = node.shadowRoot ? node.shadowRoot.childNodes : node.childNodes;
        for (const child of children) visit(child);
        if (length === before) parts.length = beginning;
        else if (suffix) parts.push(suffix);
    };
    if (root) visit(root);
    if (structured) return parts.join('').replace(/[ \f\v]+/g,' ')
        .replace(/ *\n */g,'\n').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
    return parts.join(' ').replace(/\s+/g,' ').trim();
}"""

_CONTEXT = """node => {
    const owners = 'article,section,tr,[role=row],[role=region],fieldset,dialog[open],[role=dialog],[role=alertdialog]';
    const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
    if (!node.matches(owners)) return null;
    const named = clean((node.getAttribute('aria-labelledby') || '').split(/\\s+/)
        .map(id => node.getRootNode().getElementById(id)?.innerText || '').join(' '));
    const heading = node.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,' +
        ':scope > th[scope=row],:scope > legend');
    const label = clean(node.getAttribute('aria-label')) || named || clean(heading?.innerText);
    const kind = node.getAttribute('role') || node.tagName.toLowerCase();
    return label || kind === 'dialog' || kind === 'alertdialog' ? {kind, label: label.slice(0, 240),
        basis: node.getAttribute('aria-label') ? 'aria-label' : named ? 'aria-labelledby' : 'heading'} : null;
}"""

_RECORD_CONTEXT = """node => {
    const signature = el => {
        const role = el.getAttribute('role') || '';
        const type = el.getAttribute('itemtype') || '';
        if (type || ['ARTICLE','LI','TR'].includes(el.tagName) || ['listitem','row'].includes(role))
            return [el.tagName, role, type].join('|');
        if (el.tagName === 'DIV' && el.classList.length && el.querySelector(':scope > h2,:scope > h3,:scope > blockquote'))
            return 'DIV|' + Array.from(el.classList).sort().join(' ');
        return '';
    };
    const key = signature(node);
    if (!key || !node.parentElement || node.closest('nav,header,footer,[role=navigation]')) return null;
    const shown = el => el.getClientRects().length && !el.closest('[hidden],[inert],[aria-hidden=true]')
        && getComputedStyle(el).visibility === 'visible';
    const siblings = node.parentElement.children;
    if (siblings.length > 160) return null;
    const peers = Array.from(siblings).filter(el => signature(el) === key && shown(el));
    if (peers.length < 2 || !peers.includes(node)) return null;
    const boxes = peers.map(el => el.getBoundingClientRect());
    const rtl = getComputedStyle(node.parentElement).direction === 'rtl';
    const ordered = boxes.every((box, i) => {
        if (!i) return true;
        const prev = boxes[i-1];
        const sameRow = Math.abs(box.top - prev.top) <= 1;
        return box.top >= prev.bottom - 1 || sameRow && (rtl ? box.right <= prev.left + 1 : box.left >= prev.right - 1);
    });
    const text = (""" + VISIBLE_TEXT + """);
    const field = selector => {
        const nodes = Array.from(node.querySelectorAll(selector)).filter(shown);
        return nodes.length === 1 ? text(nodes[0]) : '';
    };
    const subject = field('[itemprop~=author]');
    const quotation = subject && field('[itemprop~=text],blockquote');
    const title = field('[itemprop~=name],h2,h3');
    return {kind: quotation ? 'quotation' : 'record', basis: 'repeated',
        label: (subject || title).slice(0, 240), ...(ordered ? {ordinal: peers.indexOf(node) + 1} : {}),
        ...(quotation && quotation.length <= 1024 ? {quotation, subject} : {})};
}"""

ELEMENT_METADATA = """el => {
    const query = (""" + QUERY_TARGET + """)(el);
    const observeContext = (""" + _CONTEXT + """);
    const observeRecord = (""" + _RECORD_CONTEXT + """);
    let context = null, contextNode = null;
    for (let node = el.parentElement; node; node = node.parentElement) {
        const repeated = observeRecord(node), named = observeContext(node);
        context = repeated && named ? {...repeated, label: named.label} : repeated || named;
        if (!context) continue;
        contextNode = node;
        break;
    }
    let recordLink = false;
    if (context?.basis === 'repeated' && el.matches('a[href]:not([download])')) {
        const url = URL.canParse(el.href, document.baseURI) ? new URL(el.href, document.baseURI) : null;
        const authorLink = el.matches('[itemprop~=author]') || Boolean(el.parentElement?.querySelector(':scope > [itemprop~=author]'));
        recordLink = url !== null && /^https?:$/.test(url.protocol) && url.origin === location.origin
            && !url.username && !url.password && (url.pathname !== location.pathname || url.search !== location.search)
            && (el.target || document.querySelector('base[target]')?.target || '_self') === '_self'
            && (context.kind !== 'quotation' || authorLink);
    }
    return {contextNode, queryNode: query.node, metadata: {
        actionable: el.matches(""" + json.dumps(ACTIONABLE_CANDIDATES) + """),
        tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
        name: el.getAttribute('aria-label') || el.getAttribute('title') ||
              Array.from(el.labels || [], label => label.innerText).join(' ') ||
              el.getAttribute('placeholder') || el.innerText || '',
        type: el.getAttribute('type') || '',
        value: el.type === 'password' ? null : ('value' in el ? el.value : null),
        disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
        checked: 'checked' in el ? el.checked : null, context,
        query_relation: query.node ? query.relation : '', record_link: recordLink
        ,semantic_identity: {
            id: el.id || '', href: el.getAttribute('href') || '', controls: el.getAttribute('aria-controls') || '',
            records: Array.from((function*(){for(let n=el;n;n=n.parentElement)
                if(n.matches('[data-id],[data-key],[itemid],[aria-rowindex],[aria-posinset]')) yield n;})(),
                n => ['data-id','data-key','itemid','aria-rowindex','aria-posinset'].map(k => n.getAttribute(k)))
        }
    }};
}"""

_RECORDS = """labels => {
    const contextOf = (""" + _CONTEXT + """);
    const targetOf = (""" + QUERY_TARGET + """);
    const sourceText = (""" + VISIBLE_TEXT + """);
    const nodes = document.querySelectorAll('article,section,tr,[role=row],[role=region],fieldset');
    if (nodes.length > 240) return {rows: [], targets: []};
    const rows = [], targets = [], identities = new Map();
    for (const node of labels.includes('') ? [document.body, ...nodes] : nodes) {
        const context = node === document.body ? {label: ''} : contextOf(node);
        if (!context || !labels.includes(context.label) || !node.getClientRects().length ||
            node.closest('[hidden],[inert],[aria-hidden=true],[aria-busy=true]') ||
            getComputedStyle(node).visibility !== 'visible') continue;
        const text = sourceText(node, true);
        if (!text || text.length > 12000) continue;
        const controls = node.querySelectorAll('button,a,[role=button],[role=link]');
        const regions = [];
        if (controls.length <= 240) for (const control of controls) {
            const query = targetOf(control);
            if (!query.node || !node.contains(query.node) || !query.node.getClientRects().length ||
                query.node.closest('[hidden],[inert],[aria-hidden=true],[aria-busy=true]') ||
                getComputedStyle(query.node).visibility !== 'visible') continue;
            const region = sourceText(query.node, true);
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
        read_rows: Callable[[], object] = rows_handle.json_value
        raw = read_rows()
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
        read_data: Callable[[], object] = data_handle.json_value
        raw = read_data()
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
        read_metadata: Callable[[], object] = metadata.json_value
        raw = read_metadata()
        context_node = observed.get_property('contextNode').as_element()
        return decode_json_object(raw), context_node
    finally:
        metadata.dispose()
        observed.dispose()


def same_node(left: ElementHandle, right: ElementHandle) -> bool:
    compare: Callable[[str, JSHandle], object] = left.evaluate
    raw = compare('(node, other) => node === other', right)
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
