from __future__ import annotations

from codey.toolchain import definition as tool_defs
from codey.toolchain.openai_tools import (
    NATIVE_EXCLUDED_NAMES,
    native_tool_names,
    openai_tool_contract_hash,
    render_openai_tools,
)


def test_native_schema_names_required_and_closed() -> None:
    tools = render_openai_tools()
    by_name = {str(t["function"]["name"]): t["function"] for t in tools}
    assert "parallel" not in by_name
    assert "read_files" not in by_name
    assert set(NATIVE_EXCLUDED_NAMES) == {"parallel", "read_files"}
    for name in ("ls", "read", "search", "edit", "run", "shell"):
        assert name in by_name
    read_params = by_name["read"]["parameters"]
    assert read_params["type"] == "object"
    assert "path" in read_params["required"]
    assert read_params["additionalProperties"] is False
    edit_params = by_name["edit"]["parameters"]
    assert "path" in edit_params["required"]


def test_native_schema_sorted_and_hash_stable() -> None:
    first = render_openai_tools()
    second = render_openai_tools()
    assert [t["function"]["name"] for t in first] == sorted(t["function"]["name"] for t in first)
    assert first == second
    assert openai_tool_contract_hash() == openai_tool_contract_hash()
    assert openai_tool_contract_hash().startswith("sha256:")
    assert set(native_tool_names()) == {str(t["function"]["name"]) for t in first}
    assert all(isinstance(tool_defs.RUNTIME_TOOL_DEFINITION_BY_NAME[name], object) for name in native_tool_names())
