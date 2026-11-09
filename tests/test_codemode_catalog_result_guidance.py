from __future__ import annotations


def test_catalog_declares_tool_result_and_unwrap_once() -> None:
    from codey.codemode.catalog import build_catalog_from_specs, render_declarations
    from codey.toolchain.result_schema import read_file_output_schema
    from codey.toolchain.tool_spec import ToolSpec

    snapshot = build_catalog_from_specs({
        "open_url": ToolSpec(
            name="open_url", grant="web.read", executor="source",
            parameters=(("url", {"type": "string"}),), required=("url",),
            output_schema={"type": "string"},
        ),
        "read_file": ToolSpec(
            name="read_file", grant="project.read", executor="project",
            parameters=(("path", {"type": "string"}),), required=("path",),
            output_schema=read_file_output_schema(),
        ),
    })
    declarations = render_declarations(snapshot)
    assert "type ToolResult<T> =" in declarations
    assert "type Result<T> = Promise<ToolResult<T>>;" in declarations
    assert "declare function unwrap<T>(result: ToolResult<T>): T;" in declarations
    assert "open_url(args:" in declarations and "Result<string>" in declarations
    assert '"text": string' in declarations
    assert declarations.count("declare function unwrap") == 1


def test_codemode_description_does_not_teach_global_value_field_shapes() -> None:
    from codey.toolchain.tool_spec import tool_specs

    description = tool_specs()["codemode"].description
    assert "result.value.text" not in description
    assert "result.value.matches" not in description
    assert "unwrap" in description

