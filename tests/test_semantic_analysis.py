"""Cross-file plans remain read-only until the original patch executor applies them."""
from __future__ import annotations

import os
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from queue import SimpleQueue

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import semantic_definition as semantic
from codey.toolchain.language_server import LanguageServer, _Connection, server_for
from codey.toolchain.syntax_query import syntax_query
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from codey.workspace.revision import WorkspaceRevisionStore


@pytest.fixture
def language_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = Path(__file__).resolve().parents[1] / ".e2e-artifacts/followup-lsp-tools/node_modules/.bin"
    monkeypatch.setenv("PATH", str(cache) + os.pathsep + os.environ["PATH"])
    if server_for(Path("sample.ts")) is None:
        pytest.skip("TypeScript language server is not installed")


def fixture_project(project: Path) -> None:
    project.mkdir(exist_ok=True)
    (project / "math.ts").write_bytes(b"export function total(value: number): number { return value * 2; }\r\n")
    (project / "app.ts").write_bytes(b"import { total } from './math.js';\r\nexport const answer = total(3);\r\n")
    (project / "tsconfig.json").write_text('{"compilerOptions":{"strict":true,"module":"NodeNext","noEmit":true},"files":["math.ts","app.ts"]}', encoding="utf-8")


@pytest.mark.usefixtures("language_tools")
def test_real_typescript_definition_references_hover_and_rename_plan(tmp_path: Path) -> None:
    fixture_project(tmp_path)
    definition = semantic.semantic_definition(tmp_path, "app.ts", 2, "total")
    assert definition.ok and definition.canonical["definitions"] == [{"path": "math.ts", "line": 1, "column_utf16": 17}]
    references = semantic.semantic_definition(tmp_path, "math.ts", 1, "total", operation="references")
    assert references.ok
    locations = references.canonical["references"]
    assert isinstance(locations, list)
    assert {decode_json_object(row)["path"] for row in locations} == {"math.ts", "app.ts"}
    hover = semantic.semantic_definition(tmp_path, "math.ts", 1, "total", operation="hover")
    assert hover.ok and "number" in str(hover.canonical)
    original = (tmp_path / "math.ts").read_bytes()
    plan = semantic.semantic_definition(tmp_path, "math.ts", 1, "total", operation="rename", new_name="doubled")
    rows = plan.canonical["rename_plan"]
    assert plan.ok and isinstance(rows, list) and len(rows) == 2
    assert (tmp_path / "math.ts").read_bytes() == original


@pytest.mark.usefixtures("language_tools")
@pytest.mark.parametrize("stale", [False, True])
def test_real_semantic_plan_applies_through_versioned_patch(tmp_path: Path, stale: bool) -> None:
    project = tmp_path / "project"
    fixture_project(project)
    originals = {name: (project / name).read_bytes() for name in ("math.ts", "app.ts")}
    outcome = semantic.semantic_definition(project, "math.ts", 1, "total", operation="rename", new_name="doubled")
    assert outcome.ok
    task = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(project))
    call = ToolCall("semantic_definition", {"path": "math.ts", "line": 1, "symbol": "total", "operation": "rename", "new_name": "doubled"})
    receipt = ToolResult(call, outcome.model_text, ok=True, canonical=outcome.canonical)
    task._memory_results["plan"] = receipt
    project_execution_record(task, "plan", call, receipt, ok=True)
    if stale:
        (project / "app.ts").write_bytes(originals["app.ts"] + b"// external\r\n")
    snapshot = build_turn_snapshot(task)
    revisions = WorkspaceRevisionStore(tmp_path / "revisions")
    executor = build_execute_turn_executor(session=task, snapshot=snapshot, run_id="rename", effect_scope="task", turn=1,
        project_path=project, allow_writes=True, workspace_revision_store=revisions)
    result = execute_turn(task, [ToolCall("codemode", {"code": "return unwrap(await tools.edit({semantic_result_ref:'plan'}));"})],
        project_path=project, executors={"codemode": executor}, snapshot=snapshot, workspace_revision_store=revisions,
        run_id="rename", effect_scope="task", turn=1)[0]
    assert result.ok is not stale, result.model_text
    if stale:
        assert "stale" in result.model_text
        assert not any(row.call.name == "edit" for row in task._memory_results.values())
        assert (project / "math.ts").read_bytes() == originals["math.ts"]
    else:
        for name, original in originals.items():
            assert (project / name).read_bytes() == original.replace(b"total", b"doubled")
        edits = [row for row in task._memory_results.values() if row.call.name == "edit"]
        assert len(edits) == 2 and all(row.ok and "expected_version" in row.call.args for row in edits)


@pytest.mark.parametrize("outside", [False, True])
def test_rename_rejects_changed_workspace_and_external_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: bool) -> None:
    project = tmp_path / "project"
    fixture_project(project)
    monkeypatch.setattr(semantic, "server_for", lambda _: LanguageServer(str(tmp_path / "server"), "typescript"))

    def lookup(argv: Sequence[str], root: Path, path: Path, text: str, line: int, character: int, *,
               language: str, operation: str, new_name: str) -> JsonValue:
        if not outside:
            (root / "new.ts").write_text("external change", encoding="utf-8")
        return {"changes": {(tmp_path / "outside.ts").as_uri(): []}}

    monkeypatch.setattr(semantic, "_lookup", lookup)
    if outside:
        with pytest.raises(ValueError, match="escapes"):
            semantic.semantic_definition(project, "math.ts", 1, "total", operation="rename", new_name="doubled")
    else:
        outcome = semantic.semantic_definition(project, "math.ts", 1, "total", operation="rename", new_name="doubled")
        assert not outcome.ok and "changed" in outcome.model_text


def test_structural_queries_preserve_bytes_and_limit_results(tmp_path: Path) -> None:
    for name, content, node in (("test.py", "def alpha():\n    return 1\n\ndef beta():\n    return 2\n", "FunctionDef"),
                               ("test.ts", "function alpha() { return 1; }\nfunction beta() { return 2; }\n", "function_declaration")):
        path = tmp_path / name
        path.write_bytes(content.encode())
        result = syntax_query(tmp_path, name, node, limit=1)
        value = decode_json_object(result.canonical["value"])
        matches = value["matches"]
        assert result.ok and value["bounded"] and isinstance(matches, list) and len(matches) == 1
        assert path.read_bytes() == content.encode()


def test_diagnostic_document_uri_accepts_equivalent_drive_encoding(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    uri = path.as_uri()
    encoded = uri.replace(path.drive, path.drive.lower().replace(":", "%3A")) if path.drive else uri
    queue: SimpleQueue[JsonObject | Exception] = SimpleQueue()
    queue.put({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
        "uri": encoded, "version": 1, "diagnostics": [{"message": "original type error"}]}})
    connection = _Connection(BytesIO(), queue, uri)
    connection.receive(0.1)
    assert connection.diagnostics == [{"message": "original type error"}]


@pytest.mark.usefixtures("language_tools")
def test_real_python_diagnostics_and_references(tmp_path: Path) -> None:
    if server_for(Path("sample.py")) is None:
        pytest.skip("Pyright language server is not installed")
    (tmp_path / "sample.py").write_text('value: int = "wrong"\nprint(value)\n', encoding="utf-8")
    (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode":"strict"}', encoding="utf-8")
    diagnostics = semantic.semantic_definition(tmp_path, "sample.py", operation="diagnostics")
    assert diagnostics.ok
    result = decode_json_object(diagnostics.canonical["result"])
    assert result["complete"] is False and "reportAssignmentType" in str(result["items"])
    references = semantic.semantic_definition(tmp_path, "sample.py", 1, "value", operation="references")
    locations = references.canonical["references"]
    assert references.ok and isinstance(locations, list) and len(locations) == 2


@pytest.mark.usefixtures("language_tools")
def test_real_javascript_definition_and_cross_file_references(tmp_path: Path) -> None:
    (tmp_path / "math.js").write_text("/** @param {number} value */\nexport function total(value) { return value * 2; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("import {total} from './math.js';\nconsole.log(total(3));\n", encoding="utf-8")
    (tmp_path / "jsconfig.json").write_text('{"compilerOptions":{"checkJs":true},"files":["math.js","app.js"]}', encoding="utf-8")
    definition = semantic.semantic_definition(tmp_path, "app.js", 2, "total")
    assert definition.ok and definition.canonical["definitions"] == [{"path": "math.js", "line": 2, "column_utf16": 17}]
    references = semantic.semantic_definition(tmp_path, "math.js", 2, "total", operation="references")
    locations = references.canonical["references"]
    assert references.ok and isinstance(locations, list)
    assert {decode_json_object(row)["path"] for row in locations} == {"math.js", "app.js"}
