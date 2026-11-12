from collections.abc import Mapping

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from codey.toolchain.result_schema import schema_typescript, validate_output
from codey.toolchain.tool_spec import tool_specs
from codey.types.json_value import decode_json_object
from tests.test_browser_production_execution import URL
from tests.test_browser_production_execution import production_browser as production_browser


@pytest.mark.parametrize("code", ["null", "true", "42", "'token'", "[1,null]", "({nested:{n:1}})"])
def test_actual_browser_values_match_frozen_contract(production_browser, code: str) -> None:
    session, tools, _ = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    calls = [ToolCall("browser_navigate", {"url": URL}), ToolCall("browser_snapshot", {}),
             ToolCall("browser_eval", {"code": code}), ToolCall("browser_tab_list", {}),
             ToolCall("browser_wait", {"condition": "text", "value": "Initial", "timeout_ms": 1000}),
             ToolCall("browser_scroll", {"direction": "down", "amount": 100}),
             ToolCall("browser_tab_new", {"url": URL}), ToolCall("browser_tab_switch", {"index": 0})]
    for call in calls:
        result, ok, _ = delegate.execute(call)
        assert ok, result.model_text
        validate_output(call.name, tool_specs()[call.name].output_schema, result.canonical["value"])
        if call.name == "browser_tab_list":
            assert result.canonical["value"]["page_url"] == URL


def test_declared_browser_fields_are_typed_and_missing_data_is_rejected() -> None:
    specs = tool_specs()
    assert '"tabs": Array<' in schema_typescript(specs["browser_tab_list"].output_schema)
    assert '"value": unknown' in schema_typescript(specs["browser_eval"].output_schema)
    assert '"path": string' in schema_typescript(specs["browser_download"].output_schema)
    snapshot = specs["browser_snapshot"].output_schema
    assert 'boolean | null' in schema_typescript(snapshot)
    with pytest.raises(ValueError, match="missing required property 'tabs'"):
        validate_output("browser_tab_list", specs["browser_tab_list"].output_schema, {"text": "tabs", "page_url": URL})
    properties = specs["browser_eval"].output_schema["properties"]
    assert isinstance(properties, Mapping)
    with pytest.raises(ValueError, match="JSON cannot encode"):
        validate_output("eval", properties["value"], {"nested": object()})


@pytest.mark.parametrize(("code", "expected"), [
    ("return window.name;", ""),
    ("window.name", ""),
    ("() => { return window.name; }", ""),
    ("return\nwindow.name;", None),
    ("return window.String(42);", None),
    ("return window.name; window.name = 'changed';", None),
])
def test_browser_eval_repairs_only_a_single_property_return(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], code: str, expected: str | None,
) -> None:
    session, tools, _ = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall("browser_navigate", {"url": URL}))[1]
    result, ok, _ = delegate.execute(ToolCall("browser_eval", {"code": code}))
    assert result.call.args["code"] == code
    if expected is None:
        assert not ok and "Illegal return statement" in result.model_text
    else:
        assert ok and decode_json_object(result.canonical["value"])["value"] == expected
