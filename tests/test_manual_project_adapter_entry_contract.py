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


def _request_entry_offenders(tree: ast.Module) -> list[int]:
    """Accept constructors or callbacks forwarding a typed AgentRequest."""
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
           and node.func.id == "AgentRequest" for node in ast.walk(tree)):
        return []
    forwarded: set[int] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        parameters = {arg.arg for arg in function.args.args
                      if isinstance(arg.annotation, ast.Name)
                      and arg.annotation.id == "AgentRequest"}
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and len(node.args) == 1
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in parameters):
                forwarded.add(id(node))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"run", "run_agent"} and id(node) not in forwarded]


def test_manual_scripts_construct_or_forward_agent_request() -> None:
    """Manual entries build requests or accept them from the headless runner."""
    offenders: list[str] = []
    for path in sorted((ROOT / "tests" / "manual").glob("*.py")):
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text, filename=str(path))
        if _request_entry_offenders(tree):
            offenders.append(path.name)
    assert offenders == []


def test_typed_request_forwarding_is_an_entry() -> None:
    tree = ast.parse("def writer(request: AgentRequest):\n    return run(request)\n")
    assert _request_entry_offenders(tree) == []


def test_untyped_or_different_argument_is_not_request_forwarding() -> None:
    for source in (
        "def writer(request):\n    return run(request)\n",
        "def writer(request: AgentRequest):\n    return run(other)\n",
    ):
        assert _request_entry_offenders(ast.parse(source)) == [2]
