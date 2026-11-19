from __future__ import annotations

from pathlib import Path

import pytest

from codey.operations.coding_observations import coding_result_guidance
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_tool_args import _validate_tool_args
from codey.operations.region_edit import python_region_hints
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.runtime import READ_MAX_LINES
from codey.types.json_value import JsonObject
from codey.workspace.revision import WorkspaceRevisionStore

SOURCE = "class Store:\n    @staticmethod\n    def total(value):\n        return value + 1\n\nKEEP = 'unchanged'\n"


def _read(session: TaskSession, root: Path, *, path: str = "a.py", limit: int = READ_MAX_LINES) -> tuple[str, ToolResult]:
    result = execute_turn(session, [ToolCall("read_file", {"path": path, "limit": limit})],
                          run_id="regions", turn=len(session.executed), project_path=root)[0]
    assert result.ok, result.model_text
    return next(key for key, value in session._memory_results.items() if value is result), result


def _edit(session: TaskSession, root: Path, ref: str, **options: str | int | bool) -> ToolResult:
    args: JsonObject = {"path": "a.py", "base_result_ref": ref, "region": "Store.total",
                        "relative_indent": True, "content": "@staticmethod\ndef total(value):\n    return value + 2\n"}
    args.update(options)
    return execute_turn(session, [ToolCall("edit", args)], run_id="regions", turn=len(session.executed),
                        project_path=root, workspace_revision_store=WorkspaceRevisionStore(root.parent / (root.name + "-state")))[0]


def _session(root: Path, *, write: bool = True) -> TaskSession:
    (root / "a.py").write_text(SOURCE, encoding="utf-8")
    return TaskSession(TaskPolicy(frozenset({"project.read", "project.write"} if write else {"project.read"})))


@pytest.mark.parametrize("outer_spaces", [0, 4, 12])
def test_region_replaces_only_observed_lines_and_aligns_outer_indent(tmp_path: Path, outer_spaces: int) -> None:
    session = _session(tmp_path)
    ref, _ = _read(session, tmp_path)
    prefix = " " * outer_spaces
    replacement = prefix + "@staticmethod\n" + prefix + "def total(value):\n" + prefix + "    return value + 2\n"
    result = _edit(session, tmp_path, ref, content=replacement)
    assert result.ok, result.model_text
    assert (tmp_path / "a.py").read_text() == SOURCE.replace("value + 1", "value + 2")
    assert not _edit(session, tmp_path, ref).ok


@pytest.mark.parametrize("case", ["partial", "changed", "deleted", "other_file", "missing_ref", "readonly", "unknown_region"])
def test_region_rejects_unproven_versions_and_invalid_ranges(tmp_path: Path, case: str) -> None:
    session = _session(tmp_path, write=case != "readonly")
    (tmp_path / "b.py").write_text(SOURCE, encoding="utf-8")
    ref, _ = _read(session, tmp_path, limit=2 if case == "partial" else READ_MAX_LINES)
    if case == "changed":
        (tmp_path / "a.py").write_text(SOURCE + "external = True\n", encoding="utf-8")
    if case == "deleted":
        (tmp_path / "a.py").unlink()
    before = (tmp_path / "a.py").read_bytes() if case != "deleted" else None
    options: dict[str, str | int | bool] = {"path": "b.py"} if case == "other_file" else {}
    if case == "unknown_region":
        options["region"] = "Store.missing"
    result = _edit(session, tmp_path, "missing" if case == "missing_ref" else ref, **options)
    assert not result.ok
    assert (tmp_path / "b.py").read_text() == SOURCE
    assert ((tmp_path / "a.py").read_bytes() if case != "deleted" else None) == before


@pytest.mark.parametrize("content", ["def total(value):\nreturn value\n", "def total(value):\n\treturn value\n",
                                     'def total(value):\n    return """line one\n  exact string spaces\n"""\n'])
def test_relative_indentation_refuses_unsafe_content_atomically(tmp_path: Path, content: str) -> None:
    session = _session(tmp_path)
    ref, _ = _read(session, tmp_path)
    result = _edit(session, tmp_path, ref, content=content)
    assert not result.ok
    assert (tmp_path / "a.py").read_text() == SOURCE


def test_exact_region_preserves_multiline_strings_and_boundary_newline(tmp_path: Path) -> None:
    session = _session(tmp_path)
    ref, _ = _read(session, tmp_path)
    content = '    def total(value):\n        return """one\n  exact spaces\n"""'
    result = _edit(session, tmp_path, ref, relative_indent=False, content=content)
    assert result.ok, result.model_text
    assert (tmp_path / "a.py").read_text() == "class Store:\n" + content + "\n\nKEEP = 'unchanged'\n"


def test_ambiguous_region_name_is_rejected(tmp_path: Path) -> None:
    session = _session(tmp_path)
    source = SOURCE.replace("\nKEEP", "\n    def total(value):\n        return 3\n\nKEEP")
    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    ref, _ = _read(session, tmp_path)
    result = _edit(session, tmp_path, ref)
    assert not result.ok and "exactly one" in result.model_text
    assert (tmp_path / "a.py").read_text() == source


def test_named_region_with_old_multiline_literal_can_be_replaced_safely(tmp_path: Path) -> None:
    session = _session(tmp_path)
    source = SOURCE.replace("        return value + 1", '        text = """first\nno indentation in string\nend"""\n        return text')
    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    ref, _ = _read(session, tmp_path)
    result = _edit(session, tmp_path, ref)
    assert result.ok, result.model_text
    assert (tmp_path / "a.py").read_text() == SOURCE.replace("value + 1", "value + 2")


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_region_uses_read_file_line_numbers_preserves_eol_and_supports_deletion(tmp_path: Path, newline: bytes) -> None:
    session = _session(tmp_path)
    (tmp_path / "a.py").write_bytes(newline.join(["a = '\u2028'".encode(), b"def remove():", b"    return 1", b"last = True"]))
    ref, _ = _read(session, tmp_path)
    result = _edit(session, tmp_path, ref, region="remove", relative_indent=False, content="")
    assert result.ok, result.model_text
    assert (tmp_path / "a.py").read_bytes() == newline.join(["a = '\u2028'".encode(), b"last = True"])


@pytest.mark.parametrize("options", [
    {"region": True}, {"region": ""}, {"relative_indent": True},
    {"region": "Store.total", "relative_indent": "true"},
])
def test_region_contract_rejects_invalid_argument_combinations(options: JsonObject) -> None:
    _, error = _validate_tool_args("edit", {"path": "a.py", "content": "x", "base_result_ref": "read", **options})
    assert error


def test_read_projection_gives_decorated_method_coordinates_and_original_ref(tmp_path: Path) -> None:
    session = _session(tmp_path)
    ref, result = _read(session, tmp_path)
    assert {"region": "Store.total", "start_line": 2, "end_line": 4} in python_region_hints(SOURCE)
    guidance = coding_result_guidance(result, ref)
    assert ref in guidance and "Store.total" in guidance and "relative_indent=true" in guidance
    failed = _edit(session, tmp_path, ref, region="Store.missing")
    assert f'"limit": {READ_MAX_LINES}' in coding_result_guidance(failed, "failed-ref")


def test_codemode_uses_same_region_contract_and_receipt(tmp_path: Path) -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_protocol import build_turn_snapshot

    _session(tmp_path)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(tmp_path))
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="regions", effect_scope="task",
        turn=1, project_path=tmp_path, allow_writes=True, workspace_revision_store=revisions)
    result = execute_turn(session, [ToolCall("codemode", {
        "code": "const r=await tools.read_file({path:'a.py'}); const w=await tools.edit({path:'a.py',"
                "base_result_ref:r.result_ref,region:'Store.total',relative_indent:true,content:inputs.text}); "
                "if(!w.ok) throw Error(w.error); return 'updated';",
        "inputs": {"text": "@staticmethod\ndef total(value):\n    return value + 2\n"},
    })], run_id="regions", effect_scope="task", turn=1, project_path=tmp_path,
        executors={"codemode": executor}, snapshot=snapshot, workspace_revision_store=revisions)[0]
    assert result.ok, result.model_text
    assert (tmp_path / "a.py").read_text() == SOURCE.replace("value + 1", "value + 2")
