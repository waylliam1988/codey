"""Read-only work graph candidate, staged outside production for live evaluation.

The adapter reads committed production observations. Neither hidden graders nor
fixture page registries are dependencies. Registration uses Codey's existing
third-tool extension; query receipts use the unmodified production kernel.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections import Counter, deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from codey.operations.kernel_contracts import KernelExecutionDeps
from codey.operations.kernel_provenance import _kernel_workspace_identity_of
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.runtime.core.execution_receipt import execution_receipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import tool_spec
from codey.types.json_value import JsonObject, canonical_json_object, require_json_integer, require_json_string
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.config import load_project_config, normalize_project_relative_path, path_matches_ignored_prefix
from codey.workspace.revision import FINGERPRINT_EXCLUDED_DIRS, scan_workspace_fingerprint

TOOL_NAME = "work_context"
MAX_RESPONSE_CHARS = 7000
MAX_FILE_BYTES = 65536
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_INDEX_FILES = 400
CONSTRAINT_WORDS = re.compile(r"\b(must|only|shall|never|invalid|clamp|round|keep|preserve|return|permitted|maximum|supersed\w*|do not)\b", re.I)
STOP_WORDS = frozenset(["the", "a", "an", "is", "are", "be", "to", "for", "of", "and", "or", "in", "on", "with", "when", "from", "by", "as", "not", "only", "must", "shall", "return", "returns", "value", "values", "input", "inputs", "test", "tests", "assert", "def", "import"])


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())


@dataclass(frozen=True)
class WorkNode:
    id: str
    kind: str
    path: str
    text: str
    version: str
    start: int = 0
    end: int = 0


@dataclass(frozen=True, order=True)
class WorkEdge:
    source: str
    relation: str
    target: str


@dataclass(frozen=True)
class WorkInputs:
    ledger: ResearchLedger | None = None
    results: tuple[tuple[str, ToolResult], ...] = ()
    fingerprint: str = ""
    ignored_paths: tuple[str, ...] = ()


@dataclass
class WorkSnapshot:
    nodes: dict[str, WorkNode] = field(default_factory=dict)
    edges: set[WorkEdge] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def link(self, source: str, relation: str, target: str) -> None:
        if source in self.nodes and target in self.nodes:
            self.edges.add(WorkEdge(source, relation, target))


def production_inputs(session: TaskSession, execution: KernelExecutionDeps) -> WorkInputs:
    """Observe a committed kernel view, preserving its provenance capability."""
    if not session.policy.allows("project.read") or execution.project_path is None:
        raise PermissionError("work_context requires project.read and a project")
    with session.projection_lock:
        resources = execution.research_tools
        ledger = resources.ledger.clone() if resources is not None and session.policy.allows("web.read") else None
        results = tuple(session._memory_results.items())
    return WorkInputs(ledger, results,
        scan_workspace_fingerprint(execution.project_path, ignored_paths=execution.workspace_ignored_paths),
        execution.workspace_ignored_paths)


def file_snapshot(project: Path, inputs: WorkInputs) -> WorkSnapshot:
    root = project.resolve()
    config = load_project_config(root)
    if config.warnings:
        raise ValueError("Cannot index with invalid project scan configuration")
    ignored = (*config.config.ignored_paths, *inputs.ignored_paths)
    budget = BoundedScanBudget(max_files=MAX_INDEX_FILES, max_bytes=MAX_INDEX_BYTES)

    def allowed(path: Path) -> bool:
        return path.resolve().is_relative_to(root) and not path_matches_ignored_prefix(path.relative_to(root).as_posix(), ignored)

    snapshot = WorkSnapshot(fingerprint=inputs.fingerprint)
    for path in iter_bounded_files(root, excluded_dirs=set(FINGERPRINT_EXCLUDED_DIRS), budget=budget,
                                  allow_dir=allowed, allow_file=lambda p: allowed(p) and p.suffix in {".py", ".md", ".txt"}):
        rel = path.relative_to(root).as_posix()
        if path.stat().st_size > MAX_FILE_BYTES:
            snapshot.warnings.append(f"Skipped oversized file: {rel}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            snapshot.warnings.append(f"Unreadable file: {rel}")
            continue
        kind = "document" if path.suffix != ".py" else "code"
        snapshot.nodes[rel] = WorkNode(rel, kind, rel, text, sha(text), 0, len(text))
    if budget.limited:
        snapshot.warnings.append(budget.stop_message("Work graph"))
    return snapshot


def add_source_nodes(snapshot: WorkSnapshot, inputs: WorkInputs) -> None:
    ledger = inputs.ledger
    if ledger is None:
        return
    for source in {item.final_url: item for item in ledger.opened_sources}.values():
        text = ledger.source_text_for_url(source.final_url)
        if text:
            snapshot.nodes[source.final_url] = WorkNode(source.final_url, "read_source", source.final_url,
                text, sha(text), 0, len(text))


def add_structure(snapshot: WorkSnapshot) -> None:
    files = [node for node in snapshot.nodes.values() if node.kind in {"code", "document"}]
    for node in files:
        for other in files:
            if other.id != node.id and other.path in node.text:
                snapshot.link(node.id, "references", other.id)
        for url in re.findall(r"https?://[^\s`<>\)\]]+", node.text):
            url = url.rstrip(".,;")
            snapshot.nodes.setdefault(url, WorkNode(url, "source_locator", url, "Locator only; no source body read.", ""))
            snapshot.link(node.id, "cites", url)
        if node.kind == "code":
            add_python_nodes(snapshot, node)


def add_python_nodes(snapshot: WorkSnapshot, node: WorkNode) -> None:
    try:
        tree = ast.parse(node.text)
    except SyntaxError:
        snapshot.warnings.append(f"Cannot parse current Python: {node.path}")
        return
    lines = node.text.splitlines(keepends=True)
    for item in ast.walk(tree):
        modules: list[str] = []
        if isinstance(item, ast.ImportFrom) and item.module:
            modules.append(item.module)
        elif isinstance(item, ast.Import):
            modules.extend(alias.name for alias in item.names)
        for module in modules:
            target = module.replace(".", "/") + ".py"
            snapshot.link(node.id, "imports", target)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = sum(map(len, lines[:item.lineno - 1]))
            end = sum(map(len, lines[:item.end_lineno or item.lineno]))
            key = f"{node.id}:{item.lineno}:{item.name}"
            kind = "test" if item.name.startswith("test_") else "symbol"
            snapshot.nodes[key] = WorkNode(key, kind, node.path, node.text[start:end], node.version, start, end)
            snapshot.link(node.id, "contains", key)


def constraint_spans(text: str) -> list[tuple[int, int]]:
    """Candidate normative statements, with exact character spans, not claims."""
    return [(match.start(), match.end()) for match in re.finditer(r"[^\n]+?(?:[.!?](?=\s|$)|(?=\n)|$)", text)
            if CONSTRAINT_WORDS.search(match.group()) and not match.group().lstrip().startswith("#")]


def connected_paths(snapshot: WorkSnapshot, source: str) -> set[str]:
    adjacent = {edge.target if edge.source == source else edge.source
                for edge in snapshot.edges if edge.source == source or edge.target == source}
    # A source URL inherits explicit code locators from its citing document.
    if snapshot.nodes[source].kind == "read_source":
        adjacent |= {edge.target for edge in snapshot.edges if edge.source in adjacent and edge.relation == "references"}
    return {snapshot.nodes[key].path for key in adjacent if snapshot.nodes[key].kind in {"code", "symbol"}}


def reachable_modules(snapshot: WorkSnapshot, source: str) -> set[str]:
    visited = {source}
    queue = deque([(source, 0)])
    while queue:
        key, depth = queue.popleft()
        if depth >= 4:
            continue
        for edge in sorted(snapshot.edges):
            if edge.source == key and edge.relation == "imports" and edge.target not in visited:
                visited.add(edge.target)
                queue.append((edge.target, depth + 1))
    return visited


def add_constraints(snapshot: WorkSnapshot) -> None:
    tests = [node for node in snapshot.nodes.values() if node.kind == "test"]
    test_modules = {test.id: reachable_modules(snapshot, test.path) for test in tests}
    for source in list(snapshot.nodes.values()):
        if source.kind not in {"document", "read_source"}:
            continue
        paths = connected_paths(snapshot, source.id)
        for start, end in constraint_spans(source.text):
            key = f"constraint:{source.id}@{source.version[:12]}:{start}"
            node = WorkNode(key, "constraint_candidate", source.path, source.text[start:end], source.version, start, end)
            snapshot.nodes[key] = node
            snapshot.link(key, "quoted_from", source.id)
            for path in paths:
                snapshot.link(key, "applies_to_candidate", path)
            terms = set(tokens(node.text)) - STOP_WORDS
            for test in tests:
                if paths & test_modules[test.id]:
                    snapshot.link(key, "related_test_by_import", test.id)
                    if terms & (set(tokens(test.text)) - STOP_WORDS):
                        snapshot.link(key, "test_anchor_overlap_candidate", test.id)


def add_observations(snapshot: WorkSnapshot, inputs: WorkInputs) -> None:
    for effect_id, result in inputs.results:
        if result.call.name == TOOL_NAME:
            continue
        receipt = execution_receipt(result)
        if receipt is None:
            continue
        identity = _kernel_workspace_identity_of(result)
        current = bool(identity and identity.fingerprint == inputs.fingerprint)
        status = "passed" if result.ok and receipt.exit_code == 0 else "failed_or_unknown"
        version = identity.fingerprint if identity else ""
        key = "execution:" + effect_id
        text = (f"command={result.call.args.get('command', '')}\nstatus={status}; exit={receipt.exit_code}; "
                f"matches_current_workspace={current}; truncated={receipt.truncated or receipt.capture_truncated}\n"
                f"{receipt.output or result.model_text}")
        snapshot.nodes[key] = WorkNode(key, "execution_observation", "", text, version)
        # A real failure is an observed counterexample, not a hidden expected answer.
        for failed in re.findall(r"FAILED\s+([^\s:]+\.py)::([^\s:]+)", receipt.output or result.model_text):
            for test in list(snapshot.nodes.values()):
                if test.kind == "test" and test.path == failed[0] and test.id.endswith(":" + failed[1]):
                    snapshot.link(key, "observed_failure", test.id)
        for test in list(snapshot.nodes.values()):
            if test.kind == "test" and test.path in str(result.call.args.get("command", "")):
                snapshot.link(key, "selected_test_candidate", test.id)


def build_snapshot(project: Path, inputs: WorkInputs) -> WorkSnapshot:
    snapshot = file_snapshot(project, inputs)
    add_source_nodes(snapshot, inputs)
    add_structure(snapshot)
    add_constraints(snapshot)
    add_observations(snapshot, inputs)
    if inputs.fingerprint and scan_workspace_fingerprint(project, ignored_paths=inputs.ignored_paths) != inputs.fingerprint:
        raise ValueError("Workspace changed while indexing; query again")
    return snapshot


def ranking(nodes: dict[str, WorkNode], query: str) -> list[str]:
    docs = {key: Counter(tokens(key + " " + node.text)) for key, node in nodes.items()}
    if not docs:
        return []
    lengths = {key: sum(doc.values()) for key, doc in docs.items()}
    avg = sum(lengths.values()) / len(docs) or 1
    scores = dict.fromkeys(docs, 0.0)
    for term in set(tokens(query)):
        count = sum(term in doc for doc in docs.values())
        idf = math.log(1 + (len(docs) - count + .5) / (count + .5))
        for key, doc in docs.items():
            freq = doc[term]
            scores[key] += idf * freq * 2.2 / (freq + 1.2 * (.25 + .75 * lengths[key] / avg))
    return sorted(docs, key=lambda key: (-scores[key], key))


def query_snapshot(snapshot: WorkSnapshot, *, query: str, path: str = "", view: str = "lookup",
                   limit: int = 10, graph: bool = True) -> JsonObject:
    if not isinstance(query, str) or len(query) > 500 or not query.strip():
        raise ValueError("query must contain 1..500 characters")
    if type(limit) is not int or not 1 <= limit <= 12 or view not in {"lookup", "gaps"}:
        raise ValueError("view must be lookup/gaps and limit must be 1..12")
    normalized = normalize_project_relative_path(path) if path else ""
    if path and normalized is None:
        raise ValueError("path must stay inside the project")
    ranked = ranking(snapshot.nodes, query + " " + path)
    if graph and view == "gaps":
        seeds = [key for key in ranked if snapshot.nodes[key].kind == "constraint_candidate"]
        if normalized:
            related = {edge.source for edge in snapshot.edges if edge.relation == "applies_to_candidate" and edge.target == normalized}
            seeds.sort(key=lambda key: key not in related)
        selected = seeds[:max(1, limit // 2)]
    else:
        selected = ranked[:min(3, limit)] if graph else ranked[:limit]
    if graph:
        for key in tuple(selected):
            neighbors = {edge.target if edge.source == key else edge.source for edge in snapshot.edges
                         if edge.source == key or edge.target == key}
            selected.extend(other for other in ranked if other in neighbors and other not in selected and len(selected) < limit)
        executions = [key for key in ranked if snapshot.nodes[key].kind == "execution_observation"]
        selected = (selected[:max(0, limit - min(2, len(executions)))] + executions[:2])[:limit]
    payload: dict[str, object] = {"schema_version": 1, "workspace_fingerprint": snapshot.fingerprint,
        "warning": "Static candidate links and test passes do not prove a constraint. Inspect exact quotes, add counterexamples, and execute real tests. Snippets are not read_file receipts.",
        "index_warnings": list(snapshot.warnings), "nodes": [], "relations": [], "gaps": [], "truncated": False}
    output_nodes: list[JsonObject] = []
    included: set[str] = set()
    for key in selected:
        node = snapshot.nodes[key]
        row = canonical_json_object(asdict(node))
        if len(node.text) > 1100:
            row["text"] = node.text[:1100]
            row["text_truncated"] = True
        candidate = {**payload, "nodes": [*output_nodes, row]}
        if len(json.dumps(candidate, ensure_ascii=False)) > MAX_RESPONSE_CHARS - 1400:
            payload["truncated"] = True
            break
        output_nodes.append(row)
        included.add(key)
    payload["nodes"] = list(output_nodes)
    if graph:
        payload["relations"] = [canonical_json_object(asdict(edge)) for edge in sorted(snapshot.edges)
                                if edge.source in included and edge.target in included]
        payload["gaps"] = [{"constraint": key, "state": "behavioral_proof_not_established",
                            "anchor_test_candidates": sum(edge.source == key and edge.relation == "test_anchor_overlap_candidate"
                                                          for edge in snapshot.edges)}
                           for key in selected if key in included and snapshot.nodes[key].kind == "constraint_candidate"]
    while len(json.dumps(payload, ensure_ascii=False)) > MAX_RESPONSE_CHARS:
        relations = payload["relations"]
        if isinstance(relations, list) and relations:
            relations.pop()
            payload["truncated"] = True
        else:
            raise ValueError("Query metadata exceeds response budget")
    return canonical_json_object(payload)


@dataclass
class WorkContextService:
    project: Path
    inputs: Callable[[], WorkInputs]
    graph: bool = True
    observe: Callable[[JsonObject], None] | None = None

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            inputs = self.inputs()
            snapshot = build_snapshot(self.project, inputs)
            payload = query_snapshot(snapshot, query=require_json_string(call.args.get("query"), "query"),
                path=require_json_string(call.args.get("path", ""), "path"),
                view=require_json_string(call.args.get("view", "lookup"), "view"),
                limit=require_json_integer(call.args.get("limit", 10), "limit"), graph=self.graph)
        except (ValueError, OSError) as exc:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}")
        if self.observe is not None:
            self.observe(canonical_json_object({"call_id": call.call_id, "args": call.args, "payload": payload,
                "snapshot_nodes": len(snapshot.nodes), "snapshot_edges": len(snapshot.edges)}))
        return ToolResult(ok=True, call=call, model_text=json.dumps(payload, ensure_ascii=False), canonical={"work_graph": payload})


@contextmanager
def installed_tool(service: WorkContextService) -> Generator[None, None, None]:
    if tool_spec.spec_for_tool(TOOL_NAME) is not None:
        raise ValueError("work_context is already registered")
    registered = tool_spec.register_custom_tool(TOOL_NAME, grant="project.read", parameters=(
        ("query", {"type": "string"}), ("path", {"type": "string"}),
        ("view", {"type": "string", "enum": ["lookup", "gaps"]}), ("limit", {"type": "integer"})),
        required=("query",), description="Retrieve source-backed requirement candidates, implementation, tests and committed verification observations. Use view=gaps to inspect verification gaps, and query again after edits/tests. Candidate links are not proof. This tool neither edits nor runs commands; snippets do not replace read_file.")
    if not registered:
        raise ValueError("Could not register work_context")
    try:
        if not tool_spec.register_custom_executor(TOOL_NAME, service.execute):
            raise ValueError("Could not bind work_context executor")
        yield
    finally:
        tool_spec.unregister_custom_tool(TOOL_NAME)
