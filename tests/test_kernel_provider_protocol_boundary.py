"""The kernel consumes canonical provider turns, never model templates."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from codey.operations.kernel_protocol import normalize_turn
from codey.providers.base import AssistantTurn, ProviderToolCall


def _policy() -> SimpleNamespace:
    return SimpleNamespace(allows=lambda _grant: True)


def test_kernel_accepts_standard_assistant_turn_tool_calls() -> None:
    reply = AssistantTurn(
        text="",
        tool_calls=(
            ProviderToolCall(
                id="call-1",
                name="read_file",
                arguments={"path": "app.py"},
            ),
        ),
    )

    plan = normalize_turn(reply, policy=_policy())

    assert plan.protocol_error == ""
    assert len(plan.calls) == 1
    assert plan.calls[0].name == "read_file"
    assert plan.calls[0].call_id == "call-1"


def test_kernel_operations_do_not_import_concrete_provider_modules() -> None:
    operations = Path(__file__).resolve().parents[1] / "codey" / "operations"
    allowed_capability_modules = {"codey.providers.native_tools", "codey.providers.base"}
    concrete_imports: list[str] = []
    for path in operations.glob("kernel*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                concrete_imports.extend(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("codey.providers.")
                    and alias.name not in allowed_capability_modules
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("codey.providers.") and module not in allowed_capability_modules:
                    concrete_imports.append(module)

    assert concrete_imports == []
