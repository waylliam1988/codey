"""Shared Chat transport preserves budget, reset, cancellation and required facts."""
from contextlib import closing
from dataclasses import replace
from threading import Event

import pytest

from codey.app.context import AppContext
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from tests.support.workspace_conversation import TextConversation
from tests.test_chat_workspace_tools_and_answer_completion import NativeConversation, execute_frame, workspace_frame


@pytest.mark.parametrize("requirement", ["sources_open_required", "project_changes_required", "strict_research"])
def test_natural_answer_keeps_entry_requirement_and_one_turn_budget(tmp_path, requirement):
    frame = workspace_frame(tmp_path / "workspace", NativeConversation(AssistantTurn(text="finished")),
                            **{requirement: True})
    frame.request = replace(frame.request, max_turns=1)
    with closing(AppContext(tmp_path / "state")) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "max_turns"
    assert result.event["turns"] == 1
    assert len(frame.provider.requests) == 1


def test_custom_required_check_survives_natural_answer(tmp_path):
    frame = workspace_frame(tmp_path, NativeConversation(AssistantTurn(text="finished")))
    frame.request = replace(frame.request, max_turns=1)
    frame.entry_policy = TaskPolicy(frozenset({"control"}), required_checks=("custom_check",))
    with closing(AppContext(tmp_path / "state")) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "max_turns"
    assert frame.entry_session.policy is frame.entry_policy


def test_reset_failure_stops_before_model_or_tool_execution(tmp_path):
    provider = NativeConversation(AssistantTurn(text="must not send"))
    provider.new_chat = lambda: (_ for _ in ()).throw(RuntimeError("reset failed"))
    with closing(AppContext(tmp_path / "state")) as state:
        result, _ = execute_frame(workspace_frame(tmp_path, provider), state)
    assert result.event["stop_reason"] == "provider_failure"
    assert not provider.requests and not provider.results


@pytest.mark.parametrize("native", [False, True])
def test_text_and_native_chat_use_one_window_and_real_read(tmp_path, native):
    (tmp_path / "a.txt").write_text("actual content", encoding="utf8")
    provider = (NativeConversation(AssistantTurn(tool_calls=(ProviderToolCall("read", "read_file", {"path": "a.txt"}),)),
                                   AssistantTurn(text="Read a.txt.")) if native else
                TextConversation('{"tool":"read_file","args":{"path":"a.txt"}}', "Read a.txt."))
    frame = workspace_frame(tmp_path, provider)
    with closing(AppContext(tmp_path / "state")) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done"
    assert frame.entry_session.read_files == {"a.txt"}
    assert len(provider.requests) == result.event["turns"] == 2
    assert provider.resets == 1


def test_truncated_native_answer_does_not_complete(tmp_path):
    provider = NativeConversation(AssistantTurn(text="unfinished", finish=TurnFinish.OUTPUT_LIMIT))
    frame = workspace_frame(tmp_path, provider)
    frame.request = replace(frame.request, max_turns=1)
    with closing(AppContext(tmp_path / "state")) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] != "done"


def test_stop_after_model_response_closes_id_and_prevents_write(tmp_path):
    provider = NativeConversation(AssistantTurn(tool_calls=(ProviderToolCall("edit", "edit", {
        "path": "never.txt", "content": "bad"}),)))
    with closing(AppContext(tmp_path / "state")) as state:
        stop = Event()
        state.run_registry.stop_flag = stop
        send = provider.send_turn

        def stopped_send(prompt, tools):
            turn = send(prompt, tools)
            stop.set()
            return turn

        provider.send_turn = stopped_send
        result, _ = execute_frame(workspace_frame(tmp_path, provider), state)
    assert result.event["stop_reason"] == "stopped"
    assert not (tmp_path / "never.txt").exists()
    assert [row.call_id for row in provider.results] == ["edit"]
