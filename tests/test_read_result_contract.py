"""File content is structured data; display annotations cannot become its value."""
from __future__ import annotations

import pytest

from codey.toolchain.runtime import read_file


def test_read_value_contains_exact_page_text_and_explicit_coordinates(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"AMBER-471\r\nsecond\r\n")
    result = read_file(tmp_path, "a.txt", limit=1)
    assert result.canonical["value"] == {
        "path": "a.txt", "text": "AMBER-471\n", "start_line": 1, "end_line": 1,
        "total_lines": 2, "next_offset": 2, "text_complete": True,
    }
    assert "next offset=2" in result.model_text
    assert "next offset" not in result.canonical["value"]["text"]


def test_empty_and_clipped_reads_never_invent_complete_text(tmp_path):
    from codey.toolchain.runtime import READ_MAX_CHARS

    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    empty = read_file(tmp_path, "empty.txt").canonical["value"]
    assert empty["text"] == "" and empty["end_line"] == 0 and empty["text_complete"]
    (tmp_path / "long.txt").write_text("x" * (READ_MAX_CHARS + 1), encoding="utf-8")
    clipped = read_file(tmp_path, "long.txt").canonical["value"]
    assert not clipped["text_complete"]
    assert clipped["next_offset"] == 0


def test_host_requires_declared_structured_value_without_display_fallback(tmp_path):
    from codey.codemode.catalog import build_codemode_catalog
    from codey.codemode.host import CodeModeHost
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolResult

    snapshot = build_turn_snapshot(TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"}))))
    catalog = build_codemode_catalog(snapshot.frozen_specs)
    host = CodeModeHost(execution_id="cm", catalog=catalog.entries,
                        execute=lambda call: ToolResult(call, "AMBER-471\n", ok=True))
    with pytest.raises(ValueError, match="structured result"):
        host.invoke("read_file", {"path": "a.txt"})


def test_frozen_read_result_schema_is_advertised_to_the_actual_model():
    from codey.operations.kernel_preparation import prepare_kernel_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    prepared = prepare_kernel_turn(session, user_task="read", context_text="", task_guidance="", native=True)
    read = next(spec for spec in prepared.snapshot.frozen_specs if spec.name == "read_file")
    assert read.output_schema["properties"]["text"]["type"] == "string"
    with pytest.raises(TypeError):
        read.output_schema["properties"]["text"]["type"] = "array"
    codemode = next(spec for spec in prepared.snapshot.frozen_specs if spec.name == "codemode")
    assert '"text": string' in codemode.declarations
    assert "tools.read_file" in codemode.description


@pytest.mark.parametrize("content,offset,line,expected", [
    ("first\n第二行\n", 2, 2, "第二行"),
    ("first\n第二行\n", 2, 1, None),
    ("first\n", 1, 2, None), ("", 1, 1, None),
    ("a\vb\nlast", 1, 1, "a\vb"), ("\n", 1, 1, ""),
    ("x" * 16_001, 1, 1, None),
])
def test_absolute_line_selection_only_accepts_complete_observed_lines(tmp_path, content, offset, line, expected):
    from codey.toolchain.result_schema import read_page_line

    (tmp_path / "a.txt").write_text(content, encoding="utf-8")
    page = read_file(tmp_path, "a.txt", offset=offset).canonical["value"]
    assert read_page_line(page, line) == expected


@pytest.mark.parametrize("field,value", [("start_line", True), ("text", ["fake"]),
                                         ("text_complete", 1), ("extra", "data")])
def test_malformed_structured_values_fail_schema_validation(tmp_path, field, value):
    from codey.toolchain.result_schema import read_file_output_schema, validate_output

    (tmp_path / "a.txt").write_text("observed\n", encoding="utf-8")
    page = dict(read_file(tmp_path, "a.txt").canonical["value"])
    page[field] = value
    with pytest.raises(ValueError, match="structured result"):
        validate_output("read_file", read_file_output_schema(), page)


def test_child_value_type_is_stable_when_parent_uses_a_smaller_artifact_threshold(tmp_path):
    from codey.codemode.catalog import build_codemode_catalog
    from codey.codemode.kernel import CodeModeKernelExecutor
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    (tmp_path / "a.txt").write_text("data\n", encoding="utf-8")
    snapshot = build_turn_snapshot(TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"}))))

    def execute(call):
        page = read_file(tmp_path, call.args["path"])
        return ToolResult(call, page.model_text, ok=page.ok, canonical=page.canonical)

    executor = CodeModeKernelExecutor(execution_id="cm", catalog=build_codemode_catalog(snapshot.frozen_specs),
                                      execute_child=execute, max_inline_bytes=1)
    result = executor.execute(ToolCall("codemode", {"code":
        "const r = await tools.read_file({path:'a.txt'}); return r.value.text.length;"}))
    assert result.ok, result.model_text
    assert result.canonical["value"] == 5


def test_catalog_digest_binds_the_immutable_output_schema():
    from codey.codemode.catalog import build_catalog_snapshot
    from codey.codemode.types import ToolCatalogEntry

    schema = {"type": "string"}
    entry = ToolCatalogEntry("probe", "read", output_schema=schema)
    first = build_catalog_snapshot((entry,))
    schema["type"] = "integer"
    second = build_catalog_snapshot((ToolCatalogEntry("probe", "read", output_schema=schema),))
    assert first.digest != second.digest
    assert first.entries[0].output_schema["type"] == "string"
    with pytest.raises(KeyError, match="output_schema"):
        build_catalog_snapshot(({"name": "probe"},))


def test_host_rejects_undeclared_executor_shape_and_keeps_failures_separate():
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolResult

    entry = ToolCatalogEntry("probe", "read")
    host = CodeModeHost(execution_id="cm", catalog=(entry,), execute=lambda _: {"ok": True, "value": "data"})
    with pytest.raises(TypeError, match="ToolResult"):
        host.invoke("probe", {})
    host = CodeModeHost(execution_id="cm", catalog=(entry,),
                        execute=lambda call: ToolResult(call, "failed observation", ok=False))
    assert host.invoke("probe", {}) == {"ok": False, "error": "failed observation"}
