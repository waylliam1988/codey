from codey.durable import EntryDraft, build_context


def test_multilingual_context_estimate_does_not_treat_cjk_as_ascii() -> None:
    text = "中文測試" * 40
    snapshot = build_context(
        (EntryDraft("user", "conversation-1", None, "user_message", {"text": text}),),
        token_budget=1000,
    )
    assert snapshot.token_count >= len(text)


def test_oversized_multilingual_message_keeps_a_counted_prefix() -> None:
    text = "中文測試🙂" * 100
    snapshot = build_context(
        (EntryDraft("user", "conversation-1", None, "user_message", {"text": text}),),
        token_budget=60,
        token_counter=lambda messages: 8 + sum(len(str(message["content"])) for message in messages),
    )
    assert snapshot.entry_ids == ("user",)
    prefix = str(snapshot.messages[0]["content"])
    assert prefix and len(prefix) < len(text) and text.startswith(prefix)
    assert snapshot.token_count <= 60


def test_every_candidate_is_counted_in_provider_message_order() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "first"}),
        EntryDraft("assistant", "conversation-1", "user", "assistant_message", {"text": "second"}),
    )

    def count(messages) -> int:
        if len(messages) == 2:
            assert [message["role"] for message in messages] == ["user", "assistant"]
        return len(messages)

    assert build_context(entries, token_budget=2, token_counter=count).entry_ids == ("user", "assistant")


def test_missing_tool_result_precedes_the_next_user_message() -> None:
    entries = (
        EntryDraft("call", "conversation-1", None, "tool_call", {"tool_calls": [{"id": "call-1"}]}),
        EntryDraft("next", "conversation-1", "call", "user_message", {"text": "continue"}),
    )
    snapshot = build_context(entries, token_budget=1000)
    assert [message["role"] for message in snapshot.messages] == ["assistant", "tool", "user"]


def test_selecting_one_result_keeps_every_result_of_the_same_assistant_call() -> None:
    entries = (
        EntryDraft("call", "conversation-1", None, "tool_call", {
            "tool_calls": [{"id": "a"}, {"id": "b"}],
        }),
        EntryDraft("result-a", "conversation-1", "call", "tool_result", {"call_id": "a", "content": "a"}),
        EntryDraft("result-b", "conversation-1", "result-a", "tool_result", {"call_id": "b", "content": "b"}),
    )
    snapshot = build_context(entries, token_budget=3, token_counter=lambda messages: len(messages))
    assert snapshot.entry_ids == ("call", "result-a", "result-b")


def test_context_budget_keeps_recent_entries_and_digest_is_stable() -> None:
    entries = tuple(
        EntryDraft(f"entry-{index}", "conversation-1", f"entry-{index - 1}" if index else None, "user_message", {"text": "x" * 40})
        for index in range(4)
    )
    first = build_context(entries, token_budget=20)
    second = build_context(entries, token_budget=20)
    assert first.entry_ids == second.entry_ids
    assert first.digest == second.digest
    assert first.omitted_entry_ids


def test_context_keeps_tool_call_with_selected_tool_result() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "read"}),
        EntryDraft("call", "conversation-1", "user", "tool_call", {"tool": "read_file"}),
        EntryDraft("result", "conversation-1", "call", "tool_result", {"content": "result"}),
    )
    snapshot = build_context(entries, token_budget=100)
    assert "call" in snapshot.entry_ids
    assert "result" in snapshot.entry_ids


def test_context_encodes_tool_result_identity_for_provider_replay() -> None:
    entries = (
        EntryDraft("call", "conversation-1", None, "tool_call", {"tool_calls": [{"id": "call-1", "name": "read"}]}),
        EntryDraft("result", "conversation-1", "call", "tool_result", {"call_id": "call-1", "content": "ok"}),
    )
    snapshot = build_context(entries, token_budget=60)
    assert snapshot.messages[-1] == {"role": "tool", "content": "ok", "tool_call_id": "call-1"}


def test_context_does_not_pair_tool_result_with_a_different_call_id() -> None:
    entries = (
        EntryDraft("call-a", "conversation-1", None, "tool_call", {"tool_calls": [{"id": "a"}]}),
        EntryDraft("call-b", "conversation-1", "call-a", "tool_call", {"tool_calls": [{"id": "b"}]}),
        EntryDraft("result-a", "conversation-1", "call-b", "tool_result", {"call_id": "a", "content": "old"}),
        EntryDraft("result-b", "conversation-1", "result-a", "tool_result", {"call_id": "missing", "content": "orphan"}),
    )
    snapshot = build_context(entries, token_budget=100)
    assert "result-a" in snapshot.entry_ids
    assert "call-a" in snapshot.entry_ids
    assert "call-b" in snapshot.entry_ids
    assert "call-b:missing-result:b" in snapshot.entry_ids
    assert "result-b" not in snapshot.entry_ids


def test_compaction_discards_entries_before_summary() -> None:
    entries = (
        EntryDraft("old", "conversation-1", None, "user_message", {"text": "old"}),
        EntryDraft("compact", "conversation-1", "old", "compaction", {"summary": {"goal": "keep"}}),
        EntryDraft("new", "conversation-1", "compact", "user_message", {"text": "new"}),
    )
    snapshot = build_context(entries, token_budget=100)
    assert snapshot.entry_ids == ("compact", "new")
    assert snapshot.messages[0]["content"] == '{"goal": "keep"}'
    assert "old" in snapshot.omitted_entry_ids


def test_context_uses_provider_token_counter_when_supplied() -> None:
    entries = tuple(
        EntryDraft(f"entry-{index}", "conversation-1", f"entry-{index - 1}" if index else None,
                   "user_message", {"text": "x"})
        for index in range(3)
    )
    calls: list[int] = []

    def count(messages) -> int:
        calls.append(len(messages))
        return len(messages) * 10

    snapshot = build_context(entries, token_budget=20, token_counter=count)
    assert snapshot.entry_ids == ("entry-1", "entry-2")
    assert snapshot.token_count == 20
    assert calls


def test_context_budget_counter_receives_provider_order() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "first"}),
        EntryDraft("assistant", "conversation-1", "user", "assistant_message", {"text": "second"}),
    )
    seen: list[tuple[dict[str, object], ...]] = []

    def count(messages: tuple[dict[str, object], ...]) -> int:
        seen.append(messages)
        return len(messages)

    snapshot = build_context(entries, token_budget=2, token_counter=count)
    assert snapshot.messages == seen[-1]
    assert [message["role"] for message in seen[-1]] == ["user", "assistant"]


def test_context_excludes_incomplete_assistant_attempts() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "do it"}),
        EntryDraft(
            "aborted", "conversation-1", "user", "assistant_message",
            {"text": "partial answer", "status": "interrupted"},
        ),
        EntryDraft("next", "conversation-1", "aborted", "user_message", {"text": "continue"}),
    )

    snapshot = build_context(entries, token_budget=100)

    assert "aborted" not in snapshot.entry_ids
    assert snapshot.messages == (
        {"role": "user", "content": "do it"},
        {"role": "user", "content": "continue"},
    )


def test_context_synthesizes_error_for_unfinished_tool_call() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "read"}),
        EntryDraft(
            "call", "conversation-1", "user", "tool_call",
            {"tool_calls": [{"id": "call-1", "name": "read"}]},
        ),
        EntryDraft("next", "conversation-1", "call", "user_message", {"text": "retry"}),
    )

    snapshot = build_context(entries, token_budget=60)

    assert "call" in snapshot.entry_ids
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "call-1"
        and message.get("is_error") is True
        for message in snapshot.messages
    )
    assert snapshot.token_count <= 60


def test_context_marks_unexecuted_user_question_as_not_authorized() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "inspect"}),
        EntryDraft(
            "question-call", "conversation-1", "user", "tool_call",
            {"tool_calls": [{"id": "question-1", "name": "ask_user"}]},
        ),
        EntryDraft("answer", "conversation-1", "question-call", "user_message", {"text": "read only"}),
    )

    snapshot = build_context(entries, token_budget=100)

    result = next(message for message in snapshot.messages if message.get("role") == "tool")
    assert result["tool_call_id"] == "question-1"
    assert result["is_error"] is True
    assert "No execution authorized" in str(result["content"])


def test_context_keeps_latest_branch_local_omit_and_replace_edits() -> None:
    entries = (
        EntryDraft("user", "conversation-1", None, "user_message", {"text": "original"}),
        EntryDraft(
            "edit-1", "conversation-1", "user", "context_edit",
            {"edits": [{"target": "user", "action": "replace", "messages": [
                {"role": "user", "content": "replacement"},
            ]}]},
        ),
        EntryDraft("assistant", "conversation-1", "edit-1", "assistant_message", {"text": "answer"}),
        EntryDraft(
            "edit-2", "conversation-1", "assistant", "context_edit",
            {"edits": [{"target": "user", "action": "omit"}]},
        ),
    )

    snapshot = build_context(entries, token_budget=100)

    assert snapshot.messages == ({"role": "assistant", "content": "answer"},)
    assert "user" in snapshot.omitted_entry_ids
