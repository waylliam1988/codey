from __future__ import annotations

import ast
from pathlib import Path


def test_production_kernel_calls_pass_a_typed_request() -> None:
    root = Path(__file__).parents[1]
    violations: list[str] = []
    paths = (path for directory in ("codey", "tests", "tools") for path in (root / directory).rglob("*.py"))
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            is_kernel_call = ((isinstance(target, ast.Name) and target.id == "run_task_kernel")
                              or (isinstance(target, ast.Attribute) and target.attr == "run_task_kernel"))
            if is_kernel_call and (len(node.keywords) != 1 or node.keywords[0].arg != "request"):
                violations.append(f"{path}:{node.lineno}")
    assert violations == []
