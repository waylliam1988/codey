from __future__ import annotations

import ast
from pathlib import Path


def test_production_kernel_calls_pass_a_typed_request() -> None:
    operations = Path(__file__).parents[1] / "codey" / "operations"
    violations: list[str] = []
    for path in sorted(operations.glob("*.py")):
        if path.name == "task_loop.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            is_kernel_call = isinstance(target, ast.Name) and target.id == "run_task_kernel"
            if is_kernel_call and not any(keyword.arg == "request" for keyword in node.keywords):
                violations.append(f"{path}:{node.lineno}")
    assert violations == []
