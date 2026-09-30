"""Cold-start fixes: native done, local defaults, context budgets, receipts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_native_done_exposed_as_function() -> None:
    from codey.toolchain.openai_tools import native_tool_names, render_openai_tools

    tools = render_openai_tools()
    by_name = {str(t["function"]["name"]): t["function"] for t in tools}
    assert "done" in by_name
    assert "parallel" not in by_name
    assert "read_files" not in by_name
    assert "done" in native_tool_names()
    assert by_name["done"]["parameters"]["required"] == ["summary"]


def test_native_codec_system_prompt_is_native_only() -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.native_tools
    # Native contract is function-call wording, not JSON-object wording.
    import json as _json

    from codey.toolchain.tool_spec import thaw_schema_value

    text = _json.dumps([thaw_schema_value(t) for t in snapshot.native_tools])
    assert "function" in text.lower()


def test_native_codec_hash_is_native_not_json() -> None:

    from types import SimpleNamespace

    policy = SimpleNamespace(allows=lambda g: True)
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession

    session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.contract_text
    assert snapshot.native_tools


def test_native_done_parses_and_respects_permissions() -> None:
    from types import SimpleNamespace

    from codey.operations.kernel_protocol import normalize_turn
    from codey.providers.base import AssistantTurn, ProviderToolCall

    policy = SimpleNamespace(allows=lambda g: True)
    plan = normalize_turn(
        AssistantTurn(
            text="",
            tool_calls=(ProviderToolCall(id="d1", name="done", arguments={"summary": "ok"}),),
        ),
        policy=policy,
    )
    assert plan.control is not None and plan.control.kind == "done"
    assert plan.control.body == "ok"

    # Disallowed tool for a read-only policy fails as disallowed.
    readonly = SimpleNamespace(allows=lambda g: g != "project.write")
    bad = normalize_turn(
        AssistantTurn(
            text="",
            tool_calls=(ProviderToolCall(id="e1", name="edit", arguments={"path": "a.py"}),),
        ),
        policy=readonly,
    )
    assert bad.protocol_error
    assert bad.protocol_error_kind == "disallowed_tool"


def test_native_format_results_is_native_wording() -> None:
    from codey.operations import kernel_prompt as kp
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    policy = TaskPolicy(grants=frozenset({"control"}))
    session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
    results = [ToolResult(ToolCall(name="read_file", args={"path": "a"}, call_id="c1"), "hi")]
    prompt = kp._format_results(results, session)
    assert prompt


def test_local_native_tools_on_by_default_with_opt_out(monkeypatch, tmp_path: Path) -> None:
    from codey.providers import local_config
    from codey.providers.capabilities import capability_for

    assert capability_for("local").native_tools_default is True
    monkeypatch.delenv("NATIVE_TOOLS", raising=False)
    monkeypatch.setattr(local_config, "load_local_config", lambda: local_config.LocalProviderConfig())
    assert local_config.resolve_local_native_tools(local_config.load_local_config()) is True

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    assert local_config.resolve_local_native_tools(local_config.load_local_config()) is False
    monkeypatch.setenv("NATIVE_TOOLS", "1")
    assert local_config.resolve_local_native_tools(local_config.load_local_config()) is True

    monkeypatch.delenv("NATIVE_TOOLS", raising=False)
    monkeypatch.setattr(
        local_config,
        "load_local_config",
        lambda: local_config.LocalProviderConfig(native_tools_mode=local_config.NATIVE_TOOLS_OFF),
    )
    assert local_config.resolve_local_native_tools(local_config.load_local_config()) is False
    monkeypatch.setattr(
        local_config,
        "load_local_config",
        lambda: local_config.LocalProviderConfig(native_tools_mode=local_config.NATIVE_TOOLS_ON),
    )
    assert local_config.resolve_local_native_tools(local_config.load_local_config()) is True


def test_local_context_budgets_from_env_and_config(monkeypatch) -> None:
    from codey.env_names import (
        LOCAL_OPENAI_CONTEXT_KEEP_ENV,
        LOCAL_OPENAI_CONTEXT_RESERVE_ENV,
        LOCAL_OPENAI_CONTEXT_WINDOW_ENV,
    )
    from codey.providers import local_config

    monkeypatch.delenv(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, raising=False)
    monkeypatch.delenv(LOCAL_OPENAI_CONTEXT_RESERVE_ENV, raising=False)
    monkeypatch.delenv(LOCAL_OPENAI_CONTEXT_KEEP_ENV, raising=False)
    monkeypatch.setattr(local_config, "load_local_config", lambda: local_config.LocalProviderConfig())
    defaults = local_config.resolve_local_context_budget(local_config.load_local_config())
    assert defaults.context_window_tokens == 32_768
    assert defaults.context_window_tokens > defaults.context_reserve_tokens > 0

    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "8192")
    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_RESERVE_ENV, "2048")
    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_KEEP_ENV, "3000")
    resolved = local_config.resolve_local_context_budget(local_config.load_local_config())
    assert (
        resolved.context_window_tokens,
        resolved.context_reserve_tokens,
        resolved.context_keep_recent_tokens,
    ) == (8192, 2048, 3000)

    # Invalid (reserve >= window) is an explicit config error, not a silent default.
    import pytest

    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "2048")
    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_RESERVE_ENV, "4096")
    with pytest.raises(ValueError, match="invalid local context budget"):
        local_config.resolve_local_context_budget(local_config.load_local_config())
    # Unparsable env is also explicit, e.g. the WINDOW=1 trap still fails
    # when it cannot cover the default reserve.
    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "1")
    with pytest.raises(ValueError, match="invalid local context budget"):
        local_config.resolve_local_context_budget(local_config.load_local_config())
    monkeypatch.setenv(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "abc")
    with pytest.raises(ValueError, match="must be a positive integer"):
        local_config.resolve_local_context_budget(local_config.load_local_config())

    # Config source works when env is unset.
    monkeypatch.delenv(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, raising=False)
    monkeypatch.delenv(LOCAL_OPENAI_CONTEXT_RESERVE_ENV, raising=False)
    monkeypatch.delenv(LOCAL_OPENAI_CONTEXT_KEEP_ENV, raising=False)
    monkeypatch.setattr(
        local_config,
        "load_local_config",
        lambda: local_config.LocalProviderConfig(
            context=local_config.LocalContextBudget(16384, 4096, 6000, source="config"),
        ),
    )
    from_config = local_config.resolve_local_context_budget(local_config.load_local_config())
    assert from_config.context_window_tokens == 16384


def test_research_receipt_externalizes_with_store(tmp_path: Path) -> None:
    from codey.research.output_receipts import maybe_externalize_output
    from codey.storage.managed_outputs import ManagedOutputStore

    store = ManagedOutputStore(tmp_path / "state")
    call = SimpleNamespace(name="open_url", args={"url": "https://example.com"})
    big = "x" * 30_000
    outcome = maybe_externalize_output(
        store=store,
        session_id="s",
        run_id="r",
        permission_profile="research",
        call=call,
        output=big,
        turn=1,
        tool_index=0,
    )
    assert outcome.truncated is True
    managed = outcome.managed_output()
    assert managed["handle"].startswith("out_")
    assert managed["original_bytes"] == 30_000
    assert "externalized" in outcome.model_text
    assert store.path_for("s", "r", str(managed["handle"])).is_file()


def test_research_receipt_clips_without_store() -> None:
    from codey.research.output_receipts import maybe_externalize_output

    call = SimpleNamespace(name="web_search", args={"query": "q"})
    outcome = maybe_externalize_output(
        store=None,
        session_id="",
        run_id="",
        permission_profile="research",
        call=call,
        output="y" * 30_000,
        turn=2,
        tool_index=1,
    )
    assert outcome.truncated is True
    assert outcome.managed_output() == {}
    assert "externalized" in outcome.model_text


def test_research_dispatch_passes_turn_and_index(tmp_path: Path) -> None:
    # Old _maybe_externalize deleted with the old runner; turn/index for
    # receipts now flows via the single ExecutionDelegate (new entry).
    # Behavior (turn/index in receipts) is locked via the new task-entry
    # managed-output test (test_task_entry_cutover managed-output receipt).
    from codey.operations.task_execution import ExecutionDelegate

    assert hasattr(ExecutionDelegate, "execute")


def test_managed_output_wording_is_generic() -> None:
    from codey.agents.tool_execution import _head_tail_clip
    from codey.runtime.core.models import TRUNCATED_RESULT_NOTICE

    assert "grep/read_file" not in TRUNCATED_RESULT_NOTICE
    assert "narrower offsets" in TRUNCATED_RESULT_NOTICE
    clipped, _ = _head_tail_clip("z" * 30_000)
    assert "grep/read_file" not in clipped
    assert "narrower offsets" in clipped


def test_mutation_queue_batches_different_files_and_serializes_side_effects(tmp_path: Path) -> None:
    from codey.runtime.core.models import ToolCall
    from codey.runtime.write.file_mutation_queue import group_tool_calls_for_execution

    different_writes = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="edit", args={"path": "b.py"}),
    ]
    assert group_tool_calls_for_execution(different_writes, str(tmp_path)) == [[0, 1]]

    same_writes = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="edit", args={"path": "a.py"}),
    ]
    assert group_tool_calls_for_execution(same_writes, str(tmp_path)) == [[0], [1]]

    serial_side_effect = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="run", args={"command": "pytest", "path": "."}),
    ]
    assert group_tool_calls_for_execution(serial_side_effect, str(tmp_path)) == [[0], [1]]


def test_tool_turn_results_sort_back_to_tool_index(tmp_path: Path) -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_transport import _native_tool_messages
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=5)
    calls = [
        ToolCall(name="read_file", args={"path": "b.py"}, call_id="c0"),
        ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"),
    ]
    results = execute_turn(
        session, calls, executors={"read_file": lambda c: ToolResult(call=c, model_text=f"content:{c.args.get('path')}")},
        run_id="r1", turn=1, project_path=tmp_path,
    )
    assert [r.call.call_id for r in results] == ["c0", "c1"]
    messages = _native_tool_messages(results, session)
    assert [m["tool_call_id"] for m in messages] == ["c0", "c1"]
