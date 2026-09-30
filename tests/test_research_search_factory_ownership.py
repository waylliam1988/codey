"""Default research search factory has one owner outside operations flow.

``task_execution`` (execution resources) must not import the research flow
to obtain a default search provider. The factory lives in
``codey.research.search_factory`` and is consumed by execution resources,
the research flow, and dispatch. The leaf imports no operations modules.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "codey" / "research" / "search_factory.py"


def imports_from(path: Path) -> set[str]:
    module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
    package_parts = module.split(".")[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                prefix = package_parts[: len(package_parts) - node.level + 1]
                base = ".".join([*prefix, *([base] if base else [])])
            if base:
                result.add(base)
    return result


def test_search_factory_module_exists_and_owns_default() -> None:
    assert FACTORY.exists(), "missing codey/research/search_factory.py"
    text = FACTORY.read_text(encoding="utf-8-sig")
    assert "def default_research_search_provider" in text
    assert "ConnectorAwareSearchProvider" in text


def test_search_factory_is_operations_leaf() -> None:
    offenders = [name for name in imports_from(FACTORY) if name.startswith("codey.operations")]
    assert offenders == []


def test_execution_and_flow_consume_factory_leaf() -> None:
    for rel in (
        "codey/operations/task_execution.py",
        "codey/operations/task_phases/dispatch.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8-sig")
        assert "codey.research.search_factory" in text, rel
    flow_text = (ROOT / "codey/operations/research_flow.py").read_text(encoding="utf-8-sig")
    assert "def default_research_search_provider" not in flow_text
    assert "codey.research.search_factory" not in flow_text


def test_default_factory_builds_connector_aware_provider() -> None:
    import contextlib

    from codey.research.search_factory import default_research_search_provider

    provider = default_research_search_provider()
    try:
        assert type(provider).__name__ == "ConnectorAwareSearchProvider"
    finally:
        close = getattr(getattr(provider, "search", provider), "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
