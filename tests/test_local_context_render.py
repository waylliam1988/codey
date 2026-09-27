"""Exercise the drawer with a small DOM to catch dropped response groups."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

DRAWER = Path(__file__).resolve().parents[1] / "codey" / "web" / "assets" / "local_context_drawer.js"


class LocalContextRenderTests(unittest.TestCase):
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
"""
        result = subprocess.run(
            ["node", "-e", script, source], capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recent experiences", result.stdout)
        self.assertIn("Please keep answers concise.", result.stdout)
        self.assertNotIn("No local context yet", result.stdout)
        self.assertIn("Add to pending review", result.stdout)


if __name__ == "__main__":
    unittest.main()
