from __future__ import annotations


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
