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
    from codey.protocols.native_openai import NativeOpenAIToolCodec

    codec = NativeOpenAIToolCodec()
    prompt = codec.system_prompt()
    assert "function calls" in prompt.lower()
    assert "call done" in prompt.lower()
    assert "JSON object" not in prompt
    assert "exactly one JSON" not in prompt


def test_native_codec_hash_is_native_not_json() -> None:
    from codey.protocols.native_openai import NativeOpenAIToolCodec
    from codey.toolchain.openai_tools import openai_tool_contract_hash

    codec = NativeOpenAIToolCodec()
    assert codec.model_tool_contract_hash() == openai_tool_contract_hash(codec._fallback.definitions)
    assert codec.model_tool_contract_hash().startswith("sha256:")
    # JSON contract hash differs (different wire format).
    assert codec.model_tool_contract_hash() != codec._fallback.model_tool_contract_hash()


def test_native_done_parses_and_respects_permissions() -> None:
    from codey.protocols.native_openai import NativeOpenAIToolCodec
    from codey.providers.base import AssistantTurn, ProviderToolCall

    codec = NativeOpenAIToolCodec()
    plan = codec.parse_turn(
        AssistantTurn(
            text="",
            tool_calls=(ProviderToolCall(id="d1", name="done", arguments={"summary": "ok"}),),
        )
    )
    assert plan.control is not None and plan.control.kind == "done"
    assert plan.control.body == "ok"

    # Disallowed tool for a read-only profile fails as disallowed.
    readonly = NativeOpenAIToolCodec(permission_profile="planning_readonly")
    bad = readonly.parse_turn(
        AssistantTurn(
            text="",
            tool_calls=(ProviderToolCall(id="e1", name="edit", arguments={"path": "a.py", "content": "x"}),),
        )
    )
    assert bad.protocol_error
    assert bad.protocol_error_kind == "disallowed_tool"


def test_native_format_results_is_native_wording() -> None:
    from codey.protocols.native_openai import NativeOpenAIToolCodec
    from codey.runtime.core.models import ToolCall, ToolResult

    codec = NativeOpenAIToolCodec()
    prompt = codec.format_results([ToolResult(ToolCall(name="read", args={"path": "a"}, call_id="c1"), "hi")])
    assert "function calls" in prompt.lower()
    assert "call done" in prompt.lower()
    assert "exactly one JSON" not in prompt


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
    from codey.knowledge.store import KnowledgeStore
    from codey.research.runner import ResearchRunner

    class _Search:
        last_connector_errors: list = []

    class _Provider:
        name = "test"

        def new_chat(self, timeout=None) -> None:
            return None

    store = KnowledgeStore(tmp_path / "vault")
    runner = ResearchRunner(_Provider(), _Search(), store, session_id="s", run_id="r")
    seen: list[tuple] = []

    def fake_web_search(query: str) -> str:
        return "ok:" + query

    runner.tools.web_search = fake_web_search  # type: ignore[method-assign]
    original = runner._maybe_externalize_research_output

    def spy(call, output: str, *, turn: int, tool_index: int, presentation_result: str = ""):
        seen.append((turn, tool_index))
        return original(call, output, turn=turn, tool_index=tool_index, presentation_result=presentation_result)

    runner._maybe_externalize_research_output = spy  # type: ignore[method-assign]
    call = SimpleNamespace(name="web_search", args={"query": "hello"})
    runner._dispatch(call, 3, 2)
    assert seen == [(3, 2)]


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
    from codey.agents.state import AgentLoopSession, LoopStagnation
    from codey.agents.tool_turn import execute_turn_tools
    from codey.policies.permissions import profile_for_name
    from codey.providers.base import AssistantTurn
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    class _Provider:
        name = "local"

        def new_chat(self, timeout=None) -> None:
            return None

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            raise AssertionError("no send")

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            raise AssertionError("no send")

        def close(self) -> None:
            return None

    from codey.agents.tools import AgentToolFns

    def read_file(root: Path, rel: str, **kwargs: object) -> ToolOutcome:
        return ToolOutcome(f"content:{rel}", True)

    def list_directory(root: Path, rel: str, **kwargs: object) -> ToolOutcome:
        return ToolOutcome("listed", True)

    session = AgentLoopSession(
        request=SimpleNamespace(managed_outputs=None, session_id="", run_id=""),
        provider=_Provider(),
        project=tmp_path,
        user_task="t",
        codec=SimpleNamespace(
            name="json",
            system_prompt=lambda: "",
            model_tool_contract_hash=lambda: "",
        ),
        max_turns=5,
        stagnant_turns=3,
        on_event=lambda e: None,
        on_shell_request=None,
        stop_flag=None,
        fresh_chat=False,
        strict_fresh_chat=False,
        change_tracker=None,
        conversation=None,
        active_provider_id="local",
        handoff="",
        project_facts=None,
        research_context="",
        project_map="",
        project_config_warnings=(),
        work_checkpoint=None,
        verification_candidates=(),
        verification_candidate_loader=None,
        coding_context_enabled=False,
        ghost_directive="",
        ghost_continuity="",
        completion_repair_context=None,
        completion_repair_context_payload=None,
        profile=profile_for_name("coding_writer"),
        tool_fns=AgentToolFns(read_file=read_file, list_directory=list_directory),  # type: ignore[arg-type]
        trace_recorder=None,
        trace=SimpleNamespace(call=lambda *a, **k: None),
        system_prompt_text="",
        project_text=str(tmp_path),
        verification_required=False,
        verification_forbidden=True,
        progress=SimpleNamespace(changed_files=set(), read_file_paths=set(), known_file_paths=set(), wrote_files=False, verification=SimpleNamespace(paths=set())),
        verification=SimpleNamespace(required=False, forbidden=True, checks_passed=False, checks_ran=(), default_reminded_epoch=0, edit_epoch=0),
        stagnation=LoopStagnation(),
        project_instructions=(),
        session_id="",
        run_id="",
        runtime_mutations=None,
        runtime_effects=None,
        tool_result_delivery=None,
        native_tools=None,
    )
    calls = [
        ToolCall(name="read", args={"path": "b.py"}, call_id="c0"),
        ToolCall(name="read", args={"path": "a.py"}, call_id="c1"),
    ]
    result = execute_turn_tools(session, calls, turn=1)
    assert [item.tool_index for item in result.turn_state.delivery_items] == [0, 1]
    from codey.protocols.native_openai import NativeOpenAIToolCodec

    messages = NativeOpenAIToolCodec.tool_messages(result.turn_state.results)
    assert [m["tool_call_id"] for m in messages] == ["c0", "c1"]
