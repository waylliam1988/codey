"""Single owner for the task grant vocabulary (stdlib-only leaf).

Both the task policy and the tool registry consume this set. The leaf
imports no other ``codey`` module so neither consumer can pull the other
in through the vocabulary.
"""

from __future__ import annotations

KNOWN_TASK_GRANTS = frozenset(
    {
        "project.read",
        "project.write",
        "project.verify",
        "shell.approval",
        "web.read",
        "browser.observe",
        "browser.interact",
        "browser.effect",
        "knowledge.read",
        "knowledge.write",
        "knowledge.link",
        "control",
    }
)

BROWSER_CAPABILITIES = ("browser.observe", "browser.interact", "browser.effect")

__all__ = ["BROWSER_CAPABILITIES", "KNOWN_TASK_GRANTS"]
