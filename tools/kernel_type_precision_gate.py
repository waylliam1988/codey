"""Protect typed kernel contracts; AST checks supplement the three type checkers."""

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
    "codey/operations/recovery.py",
    "codey/runtime/log/session_projection.py",
    "codey/runtime/log/session_view.py",
    "codey/runtime/write/mutation_line.py",
    "codey/runtime/write/provider_effects.py",
    "codey/runtime/write/tool_batches.py",
    "codey/runtime/write/delivery_recovery.py",
    "codey/runtime/write/research_publication.py",
)

SCOPED_TARGETS = {
    "codey/operations/task_entry.py": (
        "build_task_policy_for_entry", "start_task_session",
        "_entry_policy_with_recovery", "_entry_check_conflict",
    ),
    "codey/operations/context.py": ("RunFrame.entry_policy", "RunFrame.entry_session"),
    "codey/durable/checkpoint.py": ("mutate_checkpoint", "_unregistered_artifacts", "_effect_changes"),
}

CONTRACT_TARGETS = (
    "codey/policies/tool_requirement.py", "codey/policies/requirement.py",
    "codey/operations/kernel_control.py", "codey/research/report_projection.py",
    "codey/operations/task_execution.py",
    "codey/runtime/core/execution_receipt.py", "codey/operations/kernel_receipts.py",
    "codey/operations/tool_result_reader.py", "codey/operations/execution_claims.py",
    "codey/operations/source_gaps.py", "codey/toolchain/runtime.py",
    "codey/research/source_download.py", "codey/research/web_sources.py",
    "codey/operations/kernel_prompt.py", "codey/operations/task_guidance.py",
    "codey/app/history_routes.py",
    "codey/durable/entry_ownership.py",
    "codey/durable/history.py", "codey/durable/history_index.py",
    "codey/durable/history_lifetime.py", "codey/durable/history_queries.py",
    "codey/durable/sqlite_display.py", "codey/durable/sqlite_history.py",
    "codey/durable/contracts.py",
    "codey/types/json_value.py", "codey/providers/base.py",
    "codey/providers/requests.py", "codey/providers/request_codec.py",
    "codey/providers/transport.py", "codey/providers/compat_transport.py",
    "codey/providers/text_result_views.py", "codey/operations/kernel_recovery_context.py",
    "codey/operations/completion_inputs.py", "codey/operations/completion_context_codec.py",
    "codey/runtime/core/verification_observation.py", "codey/runtime/core/execution_record.py",
    "codey/research/passages.py", "codey/research/ledger.py", "codey/research/ledger_receipts.py",
    "codey/research/receipt_types.py", "codey/research/source_gateway.py", "codey/research/staging.py",
    "codey/research/finding_report.py", "codey/research/answer_report.py", "codey/research/report_values.py",
    "codey/research/report_proposal.py", "codey/research/answer_contract.py", "codey/research/answer_values.py",
    "codey/research/answer_archive.py", "codey/research/knowledge_updates.py",
    "codey/research/knowledge_update_types.py", "codey/research/tool_args.py",
    "codey/research/tool_contract.py", "codey/research/knowledge_schema.py",
    "codey/operations/source_execution.py", "codey/research/source_locators.py",
    "codey/research/source_search.py", "codey/research/source_snapshots.py",
    "codey/research/source_reading.py", "codey/research/source_acquisition.py", "codey/research/tool_output.py",
    "codey/app/tool_history.py", "codey/codemode/catalog.py",
    "codey/research/questions.py", "codey/research/findings.py", "codey/research/references.py",
    "codey/research/audit_projection.py",
    "codey/toolchain/tool_args_repair.py",
)
_GENERICS = {"dict", "list", "tuple", "set", "frozenset", "type", "Dict", "List", "Tuple", "Set",
             "Mapping", "MutableMapping", "Sequence", "Collection", "Iterable", "Callable", "Iterator"}


def _aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    dynamic, typing_modules = {"Any"}, {"typing", "typing_extensions"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            typing_modules.update(item.asname or item.name for item in node.names
                                  if item.name in {"typing", "typing_extensions"})
        elif isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}:
            dynamic.update(item.asname or item.name for item in node.names if item.name == "Any")
    # Resolve assignment aliases too, including quoted TypeAlias expressions.
    for _ in range(len(list(ast.walk(tree)))):
        before = len(dynamic)
        for node in ast.walk(tree):
            if (isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
                    and _dynamic_count(node.value, dynamic, typing_modules,
                        quoted=isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name)
                        and node.annotation.id == "TypeAlias")):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                dynamic.update(target.id for target in targets if isinstance(target, ast.Name))
        if len(dynamic) == before:
            break
    return dynamic, typing_modules


def _dynamic_count(node: ast.AST, aliases: set[str], modules: set[str], *, quoted: bool = False) -> int:
    if isinstance(node, ast.Name):
        return int(node.id in aliases and isinstance(node.ctx, ast.Load))
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in modules and node.attr == "Any"):
        return 1
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and quoted:
        try:
            parsed = ast.parse(node.value, mode="eval")
        except (SyntaxError, ValueError):
            return 0
        # A string literal inside a parsed forward annotation must not recurse.
        if isinstance(parsed.body, ast.Constant):
            return 0
        return _dynamic_count(parsed, aliases, modules, quoted=True)
    if isinstance(node, ast.Subscript) and (
            isinstance(node.value, ast.Name) and node.value.id == "Literal"
            or isinstance(node.value, ast.Attribute) and node.value.attr == "Literal"):
        return _dynamic_count(node.value, aliases, modules)
    if isinstance(node, ast.arg):
        return _dynamic_count(node.annotation, aliases, modules, quoted=True) if node.annotation else 0
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return sum(_dynamic_count(child, aliases, modules) for child in ast.iter_child_nodes(node)
                   if child is not node.returns) + (
                       _dynamic_count(node.returns, aliases, modules, quoted=True) if node.returns else 0)
    if isinstance(node, ast.AnnAssign):
        return _dynamic_count(node.annotation, aliases, modules, quoted=True) + (
            _dynamic_count(node.value, aliases, modules,
                           quoted=isinstance(node.annotation, ast.Name) and node.annotation.id == "TypeAlias")
            if node.value else 0)
    if isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name) and node.func.id == "cast"
            or isinstance(node.func, ast.Attribute) and node.func.attr == "cast"):
        return sum(_dynamic_count(child, aliases, modules, quoted=index == 0) for index, child in enumerate(node.args))
    return sum(_dynamic_count(child, aliases, modules, quoted=quoted) for child in ast.iter_child_nodes(node))


def _selected_nodes(tree: ast.Module, symbols: tuple[str, ...]) -> tuple[ast.Module, list[str]]:
    selected: list[ast.stmt] = []
    missing: list[str] = []
    for symbol in symbols:
        body = tree.body
        found: ast.stmt | None = None
        for part in symbol.split("."):
            found = next((node for node in body if getattr(node, "name", None) == part
                          or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                          and node.target.id == part), None)
            body = found.body if isinstance(found, ast.ClassDef) else []
        if found is None:
            missing.append(f"missing protected symbol: {symbol}")
        else:
            selected.append(found)
    return ast.Module(body=selected, type_ignores=[]), missing


def source_violations(source: str, *, core: bool = True, symbols: tuple[str, ...] | None = None,
                      closed_callables: bool = False) -> list[str]:
    tree = ast.parse(source)
    aliases, modules = _aliases(tree)
    generics = set(_GENERICS)
    cast_names = {"cast"}
    callable_names = {"Callable"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"typing", "collections.abc", "typing_extensions"}:
            generics.update(item.asname or item.name for item in node.names if item.name in _GENERICS)
            cast_names.update(item.asname or item.name for item in node.names if item.name == "cast")
            callable_names.update(item.asname or item.name for item in node.names if item.name == "Callable")
    checked, errors = _selected_nodes(tree, symbols) if symbols is not None else (tree, [])
    if _dynamic_count(checked, aliases, modules):
        errors.append("explicit dynamic type (including aliases/forward annotations)")

    def annotation(node: ast.AST | None, *, subscript_base: bool = False) -> None:
        if node is None:
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                annotation(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                errors.append("invalid forward annotation")
            return
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
        if name in generics and not subscript_base:
            errors.append(f"bare generic annotation: {name}")
        if isinstance(node, ast.Subscript):
            base = node.value
            base_name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
            if (closed_callables and base_name in callable_names and isinstance(node.slice, ast.Tuple)
                    and isinstance(node.slice.elts[0], ast.Constant) and node.slice.elts[0].value is Ellipsis):
                errors.append("callback signature has unspecified parameters")
            annotation(node.value, subscript_base=True)
            if name != "Literal" and not (isinstance(node.value, ast.Name) and node.value.id == "Literal"
                                          or isinstance(node.value, ast.Attribute) and node.value.attr == "Literal"):
                annotation(node.slice)
        else:
            for child in ast.iter_child_nodes(node):
                annotation(child)

    for node in ast.walk(checked):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg:
                args.append(node.args.vararg)
            if node.args.kwarg:
                args.append(node.args.kwarg)
            if node.returns is None or any(arg.annotation is None and arg.arg not in {"self", "cls"} for arg in args):
                errors.append(f"untyped function: {node.name}")
            if core and (node.name == "__getattr__" or node.args.vararg or node.args.kwarg):
                errors.append(f"open core API: {node.name}")
            annotation(node.returns)
            for arg in args:
                annotation(arg.annotation)
                if arg.arg in {"provider", "session", "state", "settle", "execution_evidence"} and (
                        isinstance(arg.annotation, ast.Name) and arg.annotation.id == "object") and core and not (arg.arg == "provider" and node.name in {
                            "__init__", "from_provider", "adapt_provider", "bind_api_usage", "normalize_provider_reply",
                            "_explicit_reply_normalizer", "provider_reasoning"}):
                    errors.append(f"unmodeled core domain argument: {arg.arg}")
        elif isinstance(node, ast.AnnAssign):
            annotation(node.annotation)
            if isinstance(node.annotation, ast.Name) and node.annotation.id == "TypeAlias":
                annotation(node.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in cast_names
              and node.args and _dynamic_count(node.args[0], aliases, modules, quoted=True)):
            errors.append("dynamic cast target")
        elif core and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            receiver = ast.unparse(node.args[0]) if node.args else ""
            if receiver.split(".")[-1] in {"provider", "transport", "_provider", "_transport"}:
                errors.append("provider probing/dispatch outside adapter")
    return sorted(set(errors))


def _count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    aliases, modules = _aliases(tree)
    return _dynamic_count(tree, aliases, modules)


def current_counts() -> dict[str, int]:
    return {name: _count(ROOT / name) for name in TARGETS}


def changed_counts(expected: dict[str, int], actual: dict[str, int]) -> dict[str, tuple[int | None, int]]:
    return {
        name: (expected.get(name), value)
        for name, value in actual.items()
        if expected.get(name) != value
    }


def validate_counts(expected: dict[str, int], actual: dict[str, int]) -> None:
    changed = changed_counts(expected, actual)
    if changed:
        raise SystemExit(f"kernel type precision changed; update the owning contract deliberately: {changed}")


def main() -> int:
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    actual = current_counts()
    validate_counts(expected, actual)
    violations = {name: errors for name in (*TARGETS, *CONTRACT_TARGETS)
                  if (errors := source_violations((ROOT / name).read_text(encoding="utf-8-sig"), core=name in TARGETS,
                                                 closed_callables=name != "codey/operations/provider_session.py"
                                                 and name != "codey/providers/compat_transport.py"))}
    violations.update({name: errors for name, symbols in SCOPED_TARGETS.items()
                       if (errors := source_violations((ROOT / name).read_text(encoding="utf-8-sig"), symbols=symbols,
                                                      closed_callables=True))})
    if violations:
        raise SystemExit(f"kernel contract violations: {violations}")
    print("kernel Any counts are zero; typed contract escape checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
