from __future__ import annotations

import pytest


def test_data_inputs_preserve_text_without_javascript_source_interpretation():
    from codey.codemode.runtime import NodeWorkerRuntime

    data = {"text": "first\n'quoted' \"text\" \\t\r\n\u2028\u2029😀\x00last\n",
            "nested": {"items": [False, None, 0, "'); process.exit(); //"]},
            "__proto__": {"marker": "data"}}
    result = NodeWorkerRuntime().execute("return inputs;", inputs=data,
        catalog=(), invoke=lambda *_: pytest.fail("data must not execute"))
    assert result.ok, result.error
    assert result.value == data


def test_vm_inputs_are_deeply_readonly_and_use_vm_owned_objects():
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(
        "return {root: Object.getPrototypeOf(inputs) === Object.prototype, "
        "array: Object.getPrototypeOf(inputs.items) === Array.prototype, "
        "frozen: Object.isFrozen(inputs) && Object.isFrozen(inputs.items)};",
        inputs={"items": ["original"]}, catalog=(), invoke=lambda *_: None)
    assert result.ok and result.value == {"root": True, "array": True, "frozen": True}
    result = NodeWorkerRuntime().execute("'use strict'; inputs.items.push('changed'); return inputs;",
        inputs={"items": ["original"]}, catalog=(), invoke=lambda *_: None)
    assert not result.ok


def test_inputs_share_source_budget_and_invalid_source_is_not_repaired():
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits

    result = NodeWorkerRuntime().execute("return inputs;", inputs={"text": "字" * 20},
        catalog=(), invoke=lambda *_: None, limits=CodeModeLimits(source_bytes=50))
    assert not result.ok and result.error_code == "source_limit"
    result = NodeWorkerRuntime().execute("return 'first\nsecond';", inputs={"text": "first\nsecond"},
        catalog=(), invoke=lambda *_: pytest.fail("invalid JS must not execute a tool"))
    assert not result.ok and result.child_calls == 0
    assert result.error_code == "syntax_error"
    assert "successful outer JSON decoding" in result.error
    assert "codemode-program.js:1" in result.error and "inputs.<field>" in result.error


@pytest.mark.parametrize("code", ["return 1n;", "return () => 1;", "return {x:undefined};",
                                  "return NaN;", "return [Infinity];", "return Symbol('x');"])
def test_non_json_results_fail_without_string_or_null_fallback(code):
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(code, catalog=(), invoke=lambda *_: None)
    assert not result.ok and result.error_code == "result_encoding"


@pytest.mark.parametrize("code,location,kind", [
    ('return {payload: {text: undefined}};', '$["payload"]["text"]', "undefined"),
    ('return {items: [false, undefined]};', '$["items"]["1"]', "undefined"),
    ('return {"": undefined};', '$[""]', "undefined"),
    ('return {amount: Infinity};', '$["amount"]', "non-finite number"),
])
def test_invalid_nested_return_identifies_the_value_without_coercion(code, location, kind):
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(code, catalog=(), invoke=lambda *_: pytest.fail("no tool required"))
    assert not result.ok and result.error_code == "result_encoding"
    assert location in result.error and kind in result.error
    if kind == "undefined":
        assert "nonexistent property" in result.error and "declared value type" in result.error
    assert result.value is None


def test_output_budget_and_delivered_value_use_one_serialization():
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(
        "let count = 0; return {get value() { count += 1; return count; }};",
        catalog=(), invoke=lambda *_: None)
    assert result.ok and result.value == {"value": 1}


@pytest.mark.parametrize("code", ["console.log('data');", "console.error('data'); return 'ignored';"])
def test_console_output_is_an_explicit_error_without_an_output_channel(code):
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(code, catalog=(), invoke=lambda *_: None)
    assert not result.ok and result.error_code == "console_output_denied"
    assert "Return the required data" in result.error
    assert result.output == "" and result.value is None


@pytest.mark.parametrize("code", ["const x = 1;", "return;", "return undefined;"])
def test_missing_return_is_not_a_successful_null_result(code):
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(code, catalog=(), invoke=lambda *_: None)
    assert not result.ok and result.error_code == "missing_return"
    assert "missing return OR returning a nonexistent property" in result.error
    assert "result.value" in result.error


@pytest.mark.parametrize("code,value", [("return null;", None), ("return false;", False),
                                        ("return 0;", 0), ("return '';", "")])
def test_explicit_empty_or_falsy_return_values_remain_exact(code, value):
    from codey.codemode.runtime import NodeWorkerRuntime

    result = NodeWorkerRuntime().execute(code, catalog=(), invoke=lambda *_: None)
    assert result.ok and result.value == value
    assert type(result.value) is type(value)


def test_node_runtime_calls_host_and_returns_only_final_value() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    seen: list[tuple[str, dict[str, object]]] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        seen.append((name, args))
        return {"path": args["path"], "text": "class AppContext: pass"}

    runtime = NodeWorkerRuntime()
    result = runtime.execute(
        """
        const a = await tools.read_file({path: 'a.py'});
        const b = await tools.read_file({path: 'b.py'});
        return [a.text, b.text].filter(Boolean).join('\\n');
        """,
        catalog=(ToolCatalogEntry("read_file", "read", {"type": "object"}),),
        invoke=invoke,
        limits=CodeModeLimits(timeout_ms=5_000),
    )

    assert result.ok is True
    assert "AppContext" in result.value
    assert seen == [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"})]


def test_node_runtime_blocks_direct_process_access() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    result = NodeWorkerRuntime().execute(
        "return typeof process;",
        catalog=(ToolCatalogEntry("read_file", "read", {"type": "object"}),),
        invoke=lambda _name, _args: None,
        limits=CodeModeLimits(timeout_ms=5_000),
    )

    assert result.ok is False
    assert result.error_code == "sandbox_denied"


def test_node_runtime_applies_source_limit_and_blocks_vm_escape_tokens() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    catalog = (ToolCatalogEntry("read_file", "read", {"type": "object"}),)
    oversized = NodeWorkerRuntime().execute(
        "return 1;" + (" " * 100),
        catalog=catalog,
        invoke=lambda _name, _args: None,
        limits=CodeModeLimits(source_bytes=20),
    )
    assert oversized.ok is False
    assert oversized.error_code == "source_limit"

    escaped = NodeWorkerRuntime().execute(
        "return ({}).constructor.constructor('return process')();",
        catalog=catalog,
        invoke=lambda _name, _args: None,
        limits=CodeModeLimits(timeout_ms=5_000),
    )
    assert escaped.ok is False
    assert escaped.error_code == "sandbox_denied"


def test_node_runtime_allows_local_function_declarations() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    result = NodeWorkerRuntime().execute(
        "function add(a, b) { return a + b; } return add(2, 3);",
        catalog=(ToolCatalogEntry("read_file", "read", {"type": "object"}),),
        invoke=lambda _name, _args: None,
        limits=CodeModeLimits(timeout_ms=5_000),
    )
    assert result.ok is True
    assert result.value == 5


def test_node_runtime_reports_unserializable_host_values() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    result = NodeWorkerRuntime().execute(
        "return await tools.read_file({path: 'a.py'});",
        catalog=(ToolCatalogEntry("read_file", "read", {"type": "object"}),),
        invoke=lambda _name, _args: object(),
        limits=CodeModeLimits(timeout_ms=5_000),
    )
    assert result.ok is False
    assert result.error_code == "host_result_encoding"


def test_node_runtime_serializes_nested_frozen_catalog_schemas() -> None:
    from codey.codemode.catalog import build_codemode_catalog
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read"})))
    snapshot = build_turn_snapshot(session)
    catalog = build_codemode_catalog({spec.name: spec for spec in snapshot.frozen_specs})

    result = NodeWorkerRuntime().execute(
        "return await tools.read_file({path: 'a.py'});",
        catalog=catalog.entries,
        invoke=lambda _name, _args: {"ok": True, "value": "ok"},
        limits=CodeModeLimits(timeout_ms=1_000),
    )
    assert result.ok is True


def test_worker_replay_clock_preserves_date_call_and_constructor_semantics():
    from codey.codemode.runtime import NodeWorkerRuntime
    runtime = NodeWorkerRuntime()
    code = "return {now:Date.now(), time:new Date().getTime(), called:Date(), explicit:new Date(123).getTime(), random:[Math.random(),Math.random()]};"
    kwargs = dict(catalog=(),invoke=lambda _name,_args: None,replay_context={'epochMs':1000,'seed':42})
    first = runtime.execute(code,**kwargs)
    second = runtime.execute(code,**kwargs)
    assert first.ok and second.ok
    assert first.value == second.value
    assert first.value['now'] == first.value['time'] == 1000
    assert first.value['explicit'] == 123
    assert isinstance(first.value['called'],str)
