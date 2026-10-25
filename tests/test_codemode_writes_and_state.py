from __future__ import annotations

import pytest


def test_codemode_catalog_exposes_project_writes_only_when_explicit() -> None:
    from codey.codemode.catalog import build_codemode_catalog
    from codey.toolchain.tool_spec import ToolSpec

    specs = (
        ToolSpec(name="read_file", grant="project.read", executor="project", replay_class="safe"),
        ToolSpec(name="edit", grant="project.write", executor="project", replay_class="unsafe"),
        ToolSpec(name="run", grant="project.write", executor="project", replay_class="unsafe"),
        ToolSpec(name="shell", grant="shell.approval", executor="project", replay_class="unsafe"),
    )
    assert [entry.name for entry in build_codemode_catalog(specs).entries] == ["read_file"]
    writable = build_codemode_catalog(specs, allow_writes=True)
    assert [entry.name for entry in writable.entries] == ["edit", "read_file", "run"]
    assert all(entry.name != "shell" for entry in writable.entries)


def test_codemode_host_requires_explicit_write_capability() -> None:
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolResult

    writable = ToolCatalogEntry("edit", "write", namespace="project", read_only=False, grant="project.write")
    with pytest.raises(ValueError, match="allow_writes"):
        CodeModeHost(execution_id="cm", catalog=(writable,), execute=lambda _call: None)

    host = CodeModeHost(
        execution_id="cm", catalog=(writable,),
        execute=lambda call: ToolResult(call, str(call.args["path"]), ok=True), allow_writes=True,
    )
    assert host.invoke("edit", {"path": "a.py"}) == {"ok": True, "value": "a.py"}


def test_durable_codemode_state_survives_close_and_reopen(tmp_path) -> None:
    from codey.codemode.state import DurableCodeModeState
    from codey.durable import DurableSession, SQLiteDurableStore

    path = tmp_path / "durable.sqlite3"
    durable = DurableSession(SQLiteDurableStore(path))
    durable.admit_input(
        conversation_id="chat", request_id="request", task_id="task", text="run",
        provider_snapshot={"provider_id": "test"},
    )
    state = DurableCodeModeState(durable, "task")
    state.store("answer", {"ok": True})
    assert state.load("answer") == {"ok": True}
    durable.close()
    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        reopened_view = DurableCodeModeState(reopened, "task")
        assert reopened_view.load("answer") == {"ok": True}
    finally:
        reopened.close()


def test_durable_codemode_state_rejects_oversized_values() -> None:
    from codey.codemode.state import DurableCodeModeState
    from codey.durable import DurableSession, MemoryDurableStore

    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id="chat", request_id="request", task_id="task", text="run",
        provider_snapshot={"provider_id": "test"},
    )
    with pytest.raises(ValueError, match="size"):
        DurableCodeModeState(durable, "task", value_bytes=32).store("big", "x" * 100)


def test_node_worker_state_rpc_round_trip() -> None:
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.codemode.types import ToolCatalogEntry

    class State:
        def __init__(self) -> None:
            self.values: dict[str, object] = {}

        def store(self, key: str, value: object) -> None:
            self.values[key] = value

        def load(self, key: str) -> object:
            return self.values.get(key)

    state = State()
    runtime = NodeWorkerRuntime()
    result = runtime.execute(
        "await store('answer', {ok: true}); return await load('answer');",
        catalog=(ToolCatalogEntry("read_file", "read"),),
        invoke=lambda _name, _args: None,
        state_store=state,
    )
    assert result.ok is True
    assert result.value == {"ok": True}
    assert state.values == {"answer": {"ok": True}}
    second = runtime.execute(
        "return await load('answer');",
        catalog=(ToolCatalogEntry("read_file", "read"),),
        invoke=lambda _name, _args: None,
        state_store=state,
    )
    assert second.ok is True
    assert second.value == {"ok": True}
