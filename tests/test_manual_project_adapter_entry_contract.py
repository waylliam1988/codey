"""Manual A/B scripts must use the single-argument project adapter entry.

Production entry is ``run(request: AgentRequest) -> RunResult``. Manual
scripts are not collected by pytest, so a positional multi-argument call
would stay green in CI while failing at runtime with
``TypeError: too many positional arguments``. This contract locks the call
shape with AST so the drift cannot return.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def manual_run_call_offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    entry_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "codey.operations.project_adapter"
        for alias in node.names
        if alias.name == "run"
    }
    if not entry_names:
        return []
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in entry_names
        ):
            continue
        valid = (
            len(node.args) == 1
            and not isinstance(node.args[0], ast.Starred)
            and not node.keywords
        )
        if not valid:
            offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def test_manual_scripts_use_agent_request_entry() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "tests" / "manual").glob("*.py")):
        offenders.extend(manual_run_call_offenders(path))
    assert offenders == []


def test_manual_scripts_construct_agent_request() -> None:
    """Every manual script that calls run() must build an AgentRequest nearby."""
    offenders: list[str] = []
    for path in sorted((ROOT / "tests" / "manual").glob("*.py")):
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text, filename=str(path))
        calls_run = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"run", "run_agent"}
            for node in ast.walk(tree)
        )
        if calls_run and "AgentRequest(" not in text:
            offenders.append(path.name)
    assert offenders == []
