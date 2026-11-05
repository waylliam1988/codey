"""Classify explicit Codey imports and optionally probe cold imports in fresh processes."""
from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import importlib.util
import json
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = {
    "top_level_runtime": {"top_level"},
    "runtime_including_deferred": {"top_level", "deferred"},
    "including_type_checking": {"top_level", "deferred", "type_checking"},
}
FOCUS = (
    "codey.durable.checkpoint",
    "codey.operations.kernel_context",
    "codey.toolchain.tool_spec",
)


@dataclass(frozen=True, order=True)
class ImportEdge:
    source: str
    target: str
    scope: str
    line: int


class ImportVisitor(ast.NodeVisitor):
    def __init__(self, source: str, modules: dict[str, Path]) -> None:
        self.source = source
        self.modules = modules
        self.deferred = 0
        self.type_checking = 0
        self.edges: set[ImportEdge] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.deferred += 1
        self.generic_visit(node)
        self.deferred -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        type_guard = ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        self.visit(node.test)
        self.type_checking += int(type_guard)
        for child in node.body:
            self.visit(child)
        self.type_checking -= int(type_guard)
        for child in node.orelse:
            self.visit(child)

    def add(self, target: str, line: int) -> None:
        if target in self.modules:
            scope = "type_checking" if self.type_checking else "deferred" if self.deferred else "top_level"
            self.edges.add(ImportEdge(self.source, target, scope, line))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.add(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.source if self.modules[self.source].name == "__init__.py" else self.source.rpartition(".")[0]
            base = importlib.util.resolve_name("." * node.level + base, package)
        for alias in node.names:
            child = f"{base}.{alias.name}"
            self.add(child if child in self.modules else base, node.lineno)


def module_paths(root: Path) -> dict[str, Path]:
    return {
        ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__"): path
        for path in sorted((root / "codey").rglob("*.py"))
    }


def scan_imports(modules: dict[str, Path]) -> list[ImportEdge]:
    edges: set[ImportEdge] = set()
    for name, path in modules.items():
        visitor = ImportVisitor(name, modules)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)))
        edges.update(visitor.edges)
    return sorted(edges)


def components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return only strongly connected components containing at least two modules."""
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    result: list[list[str]] = []

    def visit(source: str) -> None:
        indices[source] = low[source] = len(indices)
        stack.append(source)
        active.add(source)
        for target in sorted(graph[source]):
            if target not in indices:
                visit(target)
                low[source] = min(low[source], low[target])
            elif target in active:
                low[source] = min(low[source], indices[target])
        if low[source] == indices[source]:
            group = []
            while True:
                target = stack.pop()
                active.remove(target)
                group.append(target)
                if target == source:
                    break
            if len(group) > 1:
                result.append(sorted(group))

    for source in sorted(graph):
        if source not in indices:
            visit(source)
    return sorted(result)


def static_report(modules: dict[str, Path], edges: list[ImportEdge]) -> dict[str, dict]:
    result = {}
    for name, scopes in SCOPES.items():
        graph = {module: set() for module in modules}
        for edge in edges:
            if edge.scope in scopes:
                graph[edge.source].add(edge.target)
        groups = components(graph)
        focused = [group for group in groups if set(group).intersection(FOCUS)]
        result[name] = {
            "edge_count": sum(len(targets) for targets in graph.values()),
            "scc_count": len(groups),
            "components": groups,
            "self_imports": sorted(source for source in graph if source in graph[source]),
            "focus_edges": [asdict(edge) for edge in edges if edge.scope in scopes
                            and any(edge.source in group and edge.target in group for group in focused)],
        }
    return result


def cold_import(target: str) -> dict:
    """Observe imports issued by functions while Codey module frames are initializing."""
    original = builtins.__import__
    events: dict[tuple, dict] = {}
    reentries: set[tuple[str, str, int]] = set()

    def traced(name, globals=None, locals=None, fromlist=(), level=0):
        caller = sys._getframe(1)
        source = caller.f_globals.get("__name__", "")
        base = importlib.util.resolve_name("." * level + name, (globals or {}).get("__package__")) if level else name
        if source.startswith("codey") and base.startswith("codey"):
            initializing = []
            frame = caller
            while frame is not None:
                module = frame.f_globals.get("__name__", "")
                if module.startswith("codey") and frame.f_code.co_name == "<module>":
                    initializing.append(module)
                frame = frame.f_back
            if initializing and caller.f_code.co_flags & 0x01:
                key = (source, caller.f_code.co_name, caller.f_lineno, base, tuple(fromlist or ()))
                events.setdefault(key, {
                    "source": source, "function": caller.f_code.co_name, "line": caller.f_lineno,
                    "target": base, "fromlist": list(fromlist or ()), "initializing_modules": initializing,
                })
            for requested in (base, *(f"{base}.{item}" for item in (fromlist or ()))):
                spec = getattr(sys.modules.get(requested), "__spec__", None)
                if getattr(spec, "_initializing", False):
                    reentries.add((source, requested, caller.f_lineno))
        return original(name, globals, locals, fromlist, level)

    builtins.__import__ = traced
    error = None
    try:
        importlib.import_module(target)
    except Exception:
        error = traceback.format_exc()
    finally:
        builtins.__import__ = original
    return {
        "module": target, "error": error,
        "loaded_codey_modules": sum(name == "codey" or name.startswith("codey.") for name in sys.modules),
        "initialization_time_function_imports": list(events.values()),
        "initializing_module_requests": sorted(reentries),
    }


def probe_imports(graphs: dict[str, dict]) -> list[dict]:
    targets = {"codey.durable", "codey.operations.provider_request", "codey.app.cli", "codey.app.server"}
    for group in graphs["including_type_checking"]["components"]:
        if set(group).intersection(FOCUS):
            targets.update(group)
    results = []
    for target in sorted(targets):
        child = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", target],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        if child.returncode:
            results.append({"module": target, "error": child.stderr, "returncode": child.returncode})
        else:
            results.append(json.loads(child.stdout))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-imports", action="store_true", help="Import focused modules in separate Python processes")
    parser.add_argument("--output", type=Path, help="Save the full JSON report")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        sys.path.insert(0, str(ROOT))
        print(json.dumps(cold_import(args.worker)))
        return
    modules = module_paths(ROOT)
    graphs = static_report(modules, scan_imports(modules))
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report = {"schema_version": 1, "source_head": head, "python": sys.version,
              "module_count": len(modules), "graphs": graphs,
              "cold_imports": probe_imports(graphs) if args.probe_imports else []}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = {"module_count": len(modules), "scc_counts": {k: v["scc_count"] for k, v in graphs.items()},
                   "cold_imports": len(report["cold_imports"]),
                   "cold_import_failures": sum(bool(row["error"]) for row in report["cold_imports"])}
        print(json.dumps(summary))
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
