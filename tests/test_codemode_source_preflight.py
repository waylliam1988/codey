from __future__ import annotations

import json

import pytest

from codey.codemode.kernel import CodeModeKernelExecutor
from codey.codemode.protocol import CodeModeInputError, parse_codemode_args, parse_codemode_text
from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits, ToolCatalogEntry
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject


@pytest.mark.parametrize("statement", ["const snapshot = 1;", "let x = 1;", "var x = 1;",
                                       "await tools.browser_wait({});", "return 1;", "throw 1;"])
@pytest.mark.parametrize("escape", [r"\n", r"\r", r"\r\n"])
def test_escaped_comment_newline_is_rejected_with_location(statement: str, escape: str) -> None:
    code = "const label = '订单';\n// 等待" + escape + statement
    with pytest.raises(CodeModeInputError) as raised:
        parse_codemode_args({"code": code})
    assert raised.value.code == "commented_statement"
    assert "line 2, column 6" in str(raised.value)
    assert "No child action was executed" in str(raised.value)


@pytest.mark.parametrize("code", [
    "// wait\nreturn 1;",
    "// wait\rreturn 1;",
    "// wait\u2028return 1;",
    "// wait\u2029return 1;",
    r"return '// wait\nreturn 1;';",
    r'return "https://example.com/\nreturn";',
    r"return `// wait\nreturn 1;`;",
    r"return /[//]\nreturn/;",
    r"return /\/\/ wait\nreturn/;",
    r"/* Example: \nreturn 1; */ return 1;",
    r"// Keep the literal \n in the data" + "\nreturn 1;",
    r"// Mention \nreturning prose" + "\nreturn 1;",
])
def test_source_literals_and_real_line_breaks_are_preserved(code: str) -> None:
    assert parse_codemode_args({"code": code}).code == code


def test_comment_inside_template_expression_is_still_diagnosed() -> None:
    code = 'return `${(() => { // wait' + r'\nreturn 1;' + '\n})()}`;'
    with pytest.raises(CodeModeInputError, match="commented out"):
        parse_codemode_args({"code": code})


def test_text_json_decoding_and_native_arguments_use_the_same_diagnostic() -> None:
    code = r"// Wait for the result\nawait tools.browser_wait({});"
    with pytest.raises(CodeModeInputError) as native:
        parse_codemode_args({"code": code})
    payload: JsonObject = {"tool": "codemode", "args": {"code": code}}
    with pytest.raises(CodeModeInputError) as text:
        parse_codemode_text(json.dumps(payload))
    assert str(native.value) == str(text.value)
    assert native.value.code == text.value.code


def test_entire_program_is_checked_before_any_kernel_child_action() -> None:
    children: list[ToolCall] = []

    def execute_child(call: ToolCall) -> ToolResult:
        children.append(call)
        return ToolResult(call=call, ok=True, model_text="read")

    executor = CodeModeKernelExecutor(
        execution_id="preflight-test", catalog=(ToolCatalogEntry("read_file", "read"),),
        execute_child=execute_child,
    )
    code = "await tools.read_file({path:'a.txt'});\n// Refresh" + r"\nconst snapshot = 1;" + "\nreturn snapshot;"
    failed = executor.execute(ToolCall("codemode", {"code": code}, call_id="native-call"))
    assert not failed.ok and not children
    assert failed.call.call_id == "native-call"
    assert failed.audit["error_code"] == "commented_statement"
    assert failed.audit["child_calls"] == 0

    corrected = executor.execute(ToolCall("codemode", {"code": code.replace(r"\nconst", "\nconst")}))
    assert corrected.ok and len(children) == 1


def test_runtime_entry_uses_same_preflight_before_invoking_tools() -> None:
    children: list[str] = []

    def invoke(name: str, _args: dict[str, object]) -> object:
        children.append(name)
        return "read"

    code = "await tools.read_file({});\n// Wait" + r"\nreturn 1;"
    runtime = NodeWorkerRuntime()
    result = runtime.execute(code, catalog=(ToolCatalogEntry("read_file", "read"),), invoke=invoke,
                             limits=CodeModeLimits(timeout_ms=5_000))
    assert not result.ok and result.error_code == "commented_statement"
    assert result.child_calls == 0 and not children

    literal = r"// Keep the data unchanged" + "\nreturn '// wait\\nreturn 1;';"
    result = runtime.execute(literal, catalog=(), invoke=invoke, limits=CodeModeLimits(timeout_ms=5_000))
    assert result.ok and result.value == "// wait\nreturn 1;"
