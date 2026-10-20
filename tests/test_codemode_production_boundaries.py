from __future__ import annotations

import threading

import pytest


def test_read_only_catalog_filters_write_and_control_specs() -> None:
    from codey.codemode.catalog import build_read_only_catalog
    from codey.toolchain.tool_spec import ToolSpec

    snapshot = build_read_only_catalog(
        (
            ToolSpec(name="read_file", grant="project.read", executor="project", replay_class="safe"),
            ToolSpec(name="edit", grant="project.write", executor="project", replay_class="unsafe"),
            ToolSpec(name="done", grant="control", executor="control", replay_class="safe"),
        )
    )
    assert [entry.name for entry in snapshot.entries] == ["read_file"]
    assert all(entry.read_only for entry in snapshot.entries)


def test_read_only_catalog_does_not_expose_legacy_batch_wrappers() -> None:
    from codey.codemode.catalog import build_read_only_catalog
    from codey.toolchain.tool_spec import ToolSpec

    snapshot = build_read_only_catalog(
        (
            ToolSpec(name="read_file", grant="project.read", executor="project", replay_class="safe"),
            ToolSpec(name="parallel", grant="project.read", executor="project", replay_class="safe"),
            ToolSpec(name="read_files", grant="project.read", executor="project", replay_class="safe"),
        )
    )
    assert [entry.name for entry in snapshot.entries] == ["read_file"]


def test_host_rejects_non_read_only_catalog_entry() -> None:
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry

    with pytest.raises(ValueError, match="read-only"):
        CodeModeHost(
            execution_id="cm-1",
            catalog=(ToolCatalogEntry("edit", "write", read_only=False),),
            execute=lambda _call: None,
        )


def test_runtime_executes_independent_read_calls_concurrently() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    rendezvous = threading.Barrier(2)

    def invoke(_name: str, args: dict[str, object]) -> object:
        # The barrier makes this a semantic concurrency check.  A serial
        # runtime cannot pass it, while worker startup and host scheduling
        # jitter do not affect the assertion.
        rendezvous.wait(timeout=1)
        return args["path"]

    result = NodeWorkerRuntime().execute(
        "return await Promise.all(['a.py', 'b.py'].map(path => tools.read_file({path})));",
        catalog=(ToolCatalogEntry("read_file", "read", read_only=True, parallel_safe=True),),
        invoke=invoke,
        limits=CodeModeLimits(timeout_ms=5_000, max_concurrent_calls=2),
    )
    assert result.ok is True
    assert result.value == ["a.py", "b.py"]


def test_large_codemode_value_is_written_as_artifact_reference() -> None:
    from codey.codemode.output import externalize_value

    class Store:
        def write_json_artifact(self, *, session_id: str, run_id: str, payload: object, schema_version: int) -> object:
            assert (session_id, run_id, schema_version) == ("s1", "r1", 1)
            assert payload == {"items": ["x"] * 10}
            return type("Ref", (), {"to_payload": lambda self: {"handle": "artifact_1"}})()

    value = externalize_value(
        {"items": ["x"] * 10}, store=Store(), session_id="s1", run_id="r1", max_inline_bytes=10,
    )
    assert value == {"artifact": {"handle": "artifact_1"}}


def test_worker_enforces_final_output_limit() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import CodeModeLimits, ToolCatalogEntry

    result = NodeWorkerRuntime().execute(
        "return 'x'.repeat(1000);",
        catalog=(ToolCatalogEntry("read_file", "read"),),
        invoke=lambda _name, _args: None,
        limits=CodeModeLimits(timeout_ms=5_000, max_output_bytes=100),
    )
    assert result.ok is False
    assert result.error_code == "output_limit"
