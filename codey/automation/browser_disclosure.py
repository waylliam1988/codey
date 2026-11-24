"""Bounded native disclosure observation, shared by the worker and projection."""
DETAILS = r"""({task, selected}) => {
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

    const textOf = (root, expanded, omittedNode = null) => {
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
                    if (child === omittedNode) continue;
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
        const summary = details.querySelector(':scope > summary');
        if (details.open || details.closest(owners) !== owner || !summary || !visible(summary)
            || summary.matches('[aria-disabled=true]') || summary.querySelector('a, button, input, [role=button]')
            || details.closest('[aria-busy=true]')) continue;
        const name = clean(summary.innerText);
        if (!name || name.length > 120 || !textOf(details, details, summary)) continue;
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
