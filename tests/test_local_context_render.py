"""Exercise the drawer with a small DOM to catch dropped response groups."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

DRAWER = Path(__file__).resolve().parents[1] / "codey" / "web" / "assets" / "local_context_drawer.js"


class LocalContextRenderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for drawer behavior tests")
    def test_failed_accept_refreshes_to_show_retry_action(self) -> None:
        source = DRAWER.read_text(encoding="utf-8")
        script = r"""
const vm = require('node:vm');
class Element {
  constructor() {
    this.children = []; this.textContent = ''; this.dataset = {}; this.classes = new Set();
    this.classList = { add: (x) => this.classes.add(x), remove: (x) => this.classes.delete(x), contains: (x) => this.classes.has(x) };
    this.listeners = {};
  }
  set innerHTML(_value) { this.children = []; this.textContent = ''; }
  append(...children) { for (const child of children) this.appendChild(child); }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
  closest() { return this; }
}
const ids = new Map();
function element(id) { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); }
let summaryReads = 0;
const context = {
  window: { CodeyUiState: { setDrawerOpen: (id, open) => open ? element(id).classList.add('open') : element(id).classList.remove('open') } },
  document: { createElement: () => new Element() },
  URLSearchParams, setTimeout,
  fetch: async (url) => {
    if (url.startsWith('/api/ghost/summary')) {
      summaryReads++;
      const repair = summaryReads > 1;
      return { ok: true, json: async () => ({ ok: true, available: true, enabled: true, scope: { session_id: 's1', project: '' },
        counts: { review: repair ? 0 : 1, repair: repair ? 1 : 0 },
        review: repair ? [] : [{ id: 'c1', summary: 'Preference' }],
        repair: repair ? [{ id: 'c1', summary: 'Preference' }] : [], health: {} }) };
    }
    return { ok: false, json: async () => ({ ok: false, error: 'activation failed' }) };
  },
};
vm.runInNewContext(process.argv[1], context);
context.window.CodeyLocalContextDrawer.init({ $: element, getActiveId: () => 's1', currentProjectPath: () => '' });
function allText(node) { return [node.textContent, ...node.children.flatMap(allText)].join(' '); }
(async () => {
  context.window.CodeyLocalContextDrawer.open();
  await new Promise(setImmediate);
  const row = element('local-context-body').children[0].children[1];
  row.children[1].onclick({ stopPropagation() {} });
  const menu = element('local-context-menu');
  menu.listeners.click({ target: menu.children[0] });
  await new Promise(setImmediate);
  process.stdout.write(String(summaryReads) + '\n' + allText(element('local-context-body')));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script, source], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2\n", result.stdout)
        self.assertIn("Needs attention", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for drawer behavior tests")
    def test_observations_only_are_visible_instead_of_empty_state(self) -> None:
        source = DRAWER.read_text(encoding="utf-8")
        script = r"""
const vm = require('node:vm');
class Element {
  constructor() {
    this.children = [];
    this.textContent = '';
    this.dataset = {};
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
    this.listeners = {};
  }
  set innerHTML(_value) { this.children = []; this.textContent = ''; }
  append(...children) { for (const child of children) this.appendChild(child); }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this[name] = value; }
  querySelector(selector) {
    for (const child of this.children) {
      if (selector.startsWith('.') && child.className === selector.slice(1)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  closest() { return this; }
  focus() {}
}
const ids = new Map();
function element(id) { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); }
const context = { window: {}, document: { createElement: () => new Element() } };
vm.runInNewContext(process.argv[1], context);
context.window.CodeyLocalContextDrawer.init({
  $: element, getActiveId: () => 's1', currentProjectPath: () => '',
});
context.window.CodeyLocalContextDrawer.render({
  ok: true, available: true, enabled: true,
  counts: { active: 0, review: 0, observations: 1 },
  observations: [{ id: 'r1', summary: 'Please keep answers concise.', kind: 'chat', scope_label: 'This chat' }],
  health: { associations: 'available' },
});
function allText(node) { return [node.textContent, ...node.children.flatMap(allText)].join(' '); }
process.stdout.write(allText(element('local-context-body')));
const row = element('local-context-body').children[0].children[1];
row.children[1].onclick({ stopPropagation() {} });
const menu = element('local-context-menu');
menu.listeners.click({ target: menu.children[0] });
process.stdout.write('\n' + allText(row));
context.window.CodeyLocalContextDrawer.render({
  ok: true, available: true, enabled: true,
  counts: { active: 0, review: 0, repair: 1 },
  repair: [{ id: 'c1', summary: 'reply length = concise', kind: 'Preference', scope_label: 'This chat' }],
  health: { associations: 'available' },
});
process.stdout.write('\n' + allText(element('local-context-body')));
const repairRow = element('local-context-body').children[0].children[1];
repairRow.children[1].onclick({ stopPropagation() {} });
process.stdout.write('\n' + allText(element('local-context-menu')));
"""
        result = subprocess.run(
            ["node", "-e", script, source], capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recent experiences", result.stdout)
        self.assertIn("Please keep answers concise.", result.stdout)
        self.assertNotIn("No local context yet", result.stdout)
        self.assertIn("Add to pending review", result.stdout)
        self.assertIn("Needs attention", result.stdout)
        self.assertIn("Retry", result.stdout)


if __name__ == "__main__":
    unittest.main()
