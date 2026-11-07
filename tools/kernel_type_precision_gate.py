"""Prevent the kernel contract surface from gaining new dynamic escapes.

This is an incremental gate: existing migration debt is recorded explicitly,
while new explicit ``Any`` uses in the protected modules fail review.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "kernel_type_precision_baseline.json"
TARGETS = (
    "codey/operations/kernel_contracts.py",
    "codey/operations/kernel_request_builder.py",
    "codey/operations/kernel_actions.py",
    "codey/operations/kernel_context.py",
    "codey/operations/kernel_driver.py",
    "codey/operations/kernel_execution.py",
    "codey/operations/kernel_protocol.py",
    "codey/operations/kernel_capabilities.py",
    "codey/operations/completion_context.py",
    "codey/operations/completion_gate.py",
    "codey/operations/project_completion_checks.py",
    "codey/operations/research_completion_checks.py",
    "codey/operations/provider_session.py",
    "codey/research/tools.py",
)


def _count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return sum(isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(tree))


def current_counts() -> dict[str, int]:
    return {name: _count(ROOT / name) for name in TARGETS}


def main() -> int:
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    actual = current_counts()
    if actual != expected:
        changed = {name: (expected.get(name), actual.get(name)) for name in TARGETS if expected.get(name) != actual.get(name)}
        raise SystemExit(f"kernel type precision changed; update the owning contract deliberately: {changed}")
    print("kernel type precision baseline is unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
