from __future__ import annotations

import pytest


def _catalog():
    from codey.codemode.types import ToolCatalogEntry
    from codey.toolchain.result_schema import read_file_output_schema, search_output_schema

    return (
        ToolCatalogEntry(
            "open_url", "open", {"type": "object"},
            output_schema={"type": "string"},
        ),
        ToolCatalogEntry(
            "read_file", "read", {"type": "object"},
            output_schema=read_file_output_schema(),
        ),
        ToolCatalogEntry(
            "grep", "search", {"type": "object"},
            output_schema=search_output_schema(),
        ),
    )


def _invoke(name: str, args: dict[str, object]) -> object:
    if name == "open_url":
        return {"ok": True, "value": "ARTICLE-V1", "result_ref": "task_turn_effect:source"}
    if name == "read_file":
        return {
            "ok": True,
            "value": {
                "path": str(args["path"]), "text": "FILE-V1", "start_line": 1,
                "end_line": 1, "total_lines": 1, "next_offset": 0, "text_complete": True,
            },
        }
    if name == "grep":
        return {
            "ok": True,
            "value": {
                "query": str(args["query"]), "path": "a.py", "include": "",
                "exclude": [], "literal": False, "case_sensitive": False,
                "has_more": False, "scan_complete": True, "offset": 0, "limit": 20,
                "before_context": 0, "after_context": 0, "next_offset": 0,
                "matches": [{"path": "a.py", "line": 1, "column_byte": 1, "text": "needle"}],
                "context": [],
                "coverage": {"files_admitted": 1, "bytes_admitted": 6, "oversized": 0,
                              "unreadable": 0, "budget_limited": False},
            },
        }
    raise AssertionError(name)


def test_unwrap_extracts_string_object_and_structured_values() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(
        """
        const text = unwrap(await tools.open_url({url: inputs.url}));
        const file = unwrap(await tools.read_file({path: inputs.path}));
        const page = unwrap(await tools.grep({query: inputs.query}));
        return {text, fileText: file.text, match: page.matches[0].text};
        """,
        inputs={"url": "https://example.test", "path": "a.py", "query": "needle"},
        catalog=_catalog(), invoke=_invoke,
    )
    assert result.ok, result.error
    assert result.value == {"text": "ARTICLE-V1", "fileText": "FILE-V1", "match": "needle"}


def test_unwrap_preserves_result_ref_only_on_the_original_envelope() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(
        """
        const original = await tools.open_url({url: inputs.url});
        return {text: unwrap(original), result_ref: original.result_ref};
        """,
        inputs={"url": "https://example.test"}, catalog=_catalog(), invoke=_invoke,
    )
    assert result.ok, result.error
    assert result.value == {"text": "ARTICLE-V1", "result_ref": "task_turn_effect:source"}


def test_unwrap_propagates_tool_failure_without_success_fallback() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime

    def fail(_name: str, _args: dict[str, object]) -> object:
        return {"ok": False, "error": "source unavailable"}

    result = NodeWorkerRuntime().execute(
        "return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test"}, catalog=_catalog(), invoke=fail,
    )
    assert not result.ok
    assert result.error_code == "execution_error"
    assert "source unavailable" in result.error


@pytest.mark.parametrize(
    "code",
    [
        "const result = await tools.open_url({url: inputs.url}); return result.value.text;",
        "const result = await tools.open_url({url: inputs.url}); return result.value.missing;",
    ],
)
def test_invalid_property_access_on_string_results_remains_rejected(code: str) -> None:
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(
        code, inputs={"url": "https://example.test"}, catalog=_catalog(), invoke=_invoke,
    )
    assert not result.ok
    assert result.error_code in {"result_encoding", "missing_return"}

