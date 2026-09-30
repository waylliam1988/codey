"""Kernel dependency direction locks (cold-start, no compat shims).

Locks the required import directions for the unified task kernel:

- agents never depends on operations (advisor exception is gone)
- execution resources do not depend on the research flow
- recovery results do not depend on recovery orchestration
- provider wrappers do not depend on kernel transport
- tool definitions do not depend on task policy
- local discovery/config and self-repair have a single direction each

The scanner covers top-level and function-level imports and resolves
relative imports, so a dependency hidden inside a function still fails.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
                result.update(
                    f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
                )
    return result


def depends_on(path: Path, prefix: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for name in imports_from(path))


def test_agents_never_depend_on_operations() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "codey" / "agents").rglob("*.py")
        if depends_on(path, "codey.operations")
    ]
    assert offenders == []


def test_execution_resources_do_not_depend_on_research_flow() -> None:
    path = ROOT / "codey/operations/task_execution.py"
    assert not depends_on(path, "codey.operations.research_flow")


def test_recovery_results_do_not_depend_on_recovery_orchestration() -> None:
    path = ROOT / "codey/operations/kernel_recovery_result.py"
    assert not depends_on(path, "codey.operations.kernel_recovery")


def test_provider_wrappers_do_not_depend_on_kernel_transport() -> None:
    path = ROOT / "codey/operations/provider_session.py"
    assert not depends_on(path, "codey.operations.kernel_transport")


def test_tool_definitions_do_not_depend_on_task_policy() -> None:
    path = ROOT / "codey/toolchain/tool_spec.py"
    assert not depends_on(path, "codey.policies.task_policy")


def test_local_discovery_does_not_depend_on_local_config() -> None:
    path = ROOT / "codey/providers/local_discovery.py"
    assert not depends_on(path, "codey.providers.local_config")


def test_self_repair_worker_does_not_depend_on_supervisor() -> None:
    path = ROOT / "codey/repairs/self_repair_worker.py"
    assert not depends_on(path, "codey.repairs.self_repair")
