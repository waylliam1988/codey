from types import SimpleNamespace
from unittest.mock import Mock

from codey.operations.kernel_protocol import normalize_turn
from codey.providers.base import AssistantTurn, ProviderToolCall

QUESTION = {"question": "检查到什么程度？", "options": ["只收集用例", "实际运行测试"]}


def test_question_is_a_control_and_cannot_share_a_turn_with_execution():
    call = ProviderToolCall(id="ask-call", name="ask_user", arguments=QUESTION)
    policy = SimpleNamespace(allows=lambda grant: grant == "control")
    plan = normalize_turn(AssistantTurn(tool_calls=(call,)), policy=policy)
    assert not plan.protocol_error
    assert plan.control.kind == "ask_user"
    mixed = normalize_turn(AssistantTurn(tool_calls=(call, ProviderToolCall(id="shell", name="shell", arguments={"command": "echo NO"}))))
    assert mixed.protocol_error


def test_question_event_has_both_text_and_structured_ui_projections():
    from codey.runtime.observe.events import RunEvent, render_run_event, run_event_ui_payload

    event = RunEvent("user_question", message=QUESTION["question"], metadata={"id": "q", **QUESTION})
    assert render_run_event(event) == QUESTION["question"]
    assert run_event_ui_payload("run", "chat", event)["question"]["options"] == QUESTION["options"]


def test_question_acknowledges_its_native_call_before_publishing_the_card():
    from codey.operations.kernel_question import pause_for_question

    provider = Mock()
    provider.acknowledge_tool_results.return_value = AssistantTurn()
    call = ProviderToolCall(id="ask-call", name="ask_user", arguments=QUESTION)
    reply = AssistantTurn(tool_calls=(call,))
    events = []

    def emit(event):
        assert provider.acknowledge_tool_results.called
        events.append(event)

    pause_for_question(QUESTION, provider, reply, True, [], on_event=emit, identity="run", turn=1)
    assert provider.acknowledge_tool_results.call_args.args[0][0].call_id == "ask-call"
    assert events[0].kind == "user_question"


def test_question_answer_is_durable_and_a_different_second_answer_is_rejected(tmp_path):
    import pytest

    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    ctx.durable.admit_input(
        conversation_id="chat", request_id="source", task_id="run_source",
        text="source task", provider_snapshot={"provider_id": "local"},
    )
    ctx.durable.claim_task("run_source")
    store = ctx.user_questions
    item = store.request("q", "chat", "run_source", QUESTION, {"provider": "local", "project": "", "intent": "chat"})
    accepted = store.answer("q", item["revision"], "只收集用例")
    ctx.close()
    restored_ctx = AppContext(tmp_path)
    restored = restored_ctx.user_questions
    assert restored.answer("q", item["revision"], "只收集用例") == accepted
    with pytest.raises(ValueError, match="already"):
        restored.answer("q", item["revision"], "实际运行测试")
    assert restored.get("q")["state"] == "answered"
    restored_ctx.close()


def test_pending_question_is_not_hidden_by_newer_completed_history(tmp_path):
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path)
    store = ctx.user_questions
    ctx.durable.admit_input(
        conversation_id="old-chat", request_id="old-source", task_id="old-run",
        text="source", provider_snapshot={"provider_id": "local"},
    )
    store.request("old", "old-chat", "old-run", QUESTION, {})
    # One durable task owns one question; use distinct source tasks for the
    # historical rows instead of an unowned legacy database.
    for index in range(260):
        identity = f"q{index}"
        task_id = f"r{index}"
        ctx.durable.admit_input(
            conversation_id="other", request_id=task_id, task_id=task_id,
            text="source", provider_snapshot={"provider_id": "local"},
        )
        store.request(identity, "other", task_id, QUESTION, {})
        store.cancel(identity, 1)
    assert store.pending("old-chat")["id"] == "old"
    assert any(item["id"] == "old" for item in store.list())
    ctx.close()


def test_accepted_answer_recovers_once_without_another_submission(tmp_path):
    from codey.app.context import AppContext

    ctx = AppContext(tmp_path, start_scheduler=False)
    ctx.durable.admit_input(
        conversation_id="chat",
        request_id="source-request",
        task_id="run_source",
        text="source task",
        provider_snapshot={"provider_id": "local"},
    )
    ctx.durable.claim_task("run_source")
    ctx.durable.wait_task("run_source")
    ctx.user_questions.request("q", "chat", "run_source", QUESTION, {"provider": "local", "project": "", "intent": "chat"})
    ctx.user_questions.answer("q", 1, "只收集用例")
    assert ctx.user_questions.get("q")["state"] == "answered"
    assert len(ctx.durable.tasks()) == 1
    assert ctx.durable.task("run_source").status == "pending"
    assert ctx.durable.task("run_source").state["resume_input"]["source_run_id"] == "run_source"
    ctx.close()
    reopened = AppContext(tmp_path, start_scheduler=False)
    try:
        reopened.user_questions.answer("q", 1, "只收集用例")
        assert len(reopened.durable.tasks()) == 1
        assert len(reopened.durable.entries("chat")) == 2
    finally:
        reopened.close()
