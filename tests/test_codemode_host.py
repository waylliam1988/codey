from __future__ import annotations

from types import MappingProxyType

import pytest


def test_catalog_from_frozen_specs_renders_namespaced_declarations() -> None:
    from codey.codemode.catalog import build_catalog_from_specs, render_declarations
    from codey.toolchain.tool_spec import ToolSpec

    snapshot = build_catalog_from_specs(
        {
            "read_file": ToolSpec(
                name="read_file", grant="project.read", executor="project",
                parameters=(("path", {"type": "string", "description": "Workspace-relative file."}),
                            ("limit", {"type": "integer"})), required=("path",),
                description="Read one file.", replay_class="safe",
            ),
            "codemode": ToolSpec(name="codemode", grant="project.read", executor="custom"),
        }
    )

    declarations = render_declarations(snapshot)
    assert "read_file(args:" in declarations
    assert "uses NAME's declared arguments" in declarations
    assert '"path": string' in declarations
    assert '"limit"?: number' in declarations
    assert "// read_file.path: Workspace-relative file." in declarations
    assert "codemode" not in declarations

    properties = snapshot.entries[0].parameters["properties"]
    assert isinstance(properties, MappingProxyType)
    path: object = properties["path"]
    assert isinstance(path, MappingProxyType)


def test_host_bridge_assigns_stable_child_ids_and_rejects_recursion() -> None:
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolCall, ToolResult

    received: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        received.append(call)
        return ToolResult(ok=True, call=call, model_text="ok", canonical={"value": 3})

    host = CodeModeHost(
        execution_id="cm-7",
        catalog=(ToolCatalogEntry("read_file", "read", {"type": "object"}, output_schema={"type": "integer"}),),
        execute=execute,
    )
    assert host.invoke("read_file", {"path": "a.py"}) == {"ok": True, "value": 3}
    assert received[0].call_id == "cm-7:child:0"
    with pytest.raises(ValueError, match="recursive"):
        host.invoke("codemode", {})


def test_host_rejects_two_frozen_contracts_for_the_same_name() -> None:
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolResult

    catalog = (
        ToolCatalogEntry("read_file", "Text result", output_schema={"type": "string"}),
        ToolCatalogEntry("read_file", "Numeric result", output_schema={"type": "integer"}),
    )
    with pytest.raises(ValueError, match="duplicate catalog tool: read_file"):
        CodeModeHost(
            execution_id="duplicate",
            catalog=catalog,
            execute=lambda call: ToolResult(ok=True, call=call, model_text="read"),
        )
