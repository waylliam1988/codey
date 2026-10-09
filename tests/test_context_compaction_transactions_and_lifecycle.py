"""Foreground transactions, background races and auxiliary lifecycle stay isolated."""
import threading
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import RequestContextCount


def reply(text="ok"):
    return {"choices": [{"message": {"content": text}}]}


def long_history():
    return [{"role": "user", "content": "Keep API"},
            *[{"role": "assistant", "content": f"step {i}: " + "detail " * 500} for i in range(6)],
            {"role": "user", "content": "Latest correction: keep files local"}]


def test_background_summary_preserves_appended_tail_without_waiting():
    started, finish = threading.Event(), threading.Event()
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = long_history()

    def summary(source, deadline):
        started.set()
        assert finish.wait(3)
        return "Goal: Keep API. Completed: read files. Next: verify."

    provider.summarize_context = summary
    provider.maintain_context()
    assert started.wait(2)
    with patch("codey.providers.api_transport.generate", return_value=reply()):
        provider.send("new correction")
    finish.set()
    provider.wait_for_maintenance(3)
    assert provider._messages[-2]["content"] == "new correction"
    assert any("Current project state:" in str(item) for item in provider._messages)
    provider.close()


def test_close_discards_a_late_background_result():
    started, finish = threading.Event(), threading.Event()
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = long_history()

    def summary(source, deadline):
        started.set()
        assert finish.wait(3)
        return "Goal: preserve API"

    provider.summarize_context = summary
    provider.maintain_context()
    assert started.wait(2)
    provider.close()
    finish.set()
    provider.wait_for_maintenance(3)
    assert provider._messages == []


def test_emergency_summary_failure_preserves_original_and_never_sends_main_request():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=2000)
    original = long_history()
    provider._messages = original
    provider.summarize_context = lambda *_: (_ for _ in ()).throw(RuntimeError("summary failed"))
    with patch("codey.providers.api_transport.generate", side_effect=AssertionError("must not generate")), pytest.raises(RuntimeError, match="summary failed"):
        provider.send("continue")
    assert provider._messages == original


def test_summary_usage_is_separate_and_final_candidate_is_recounted():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=2000)
    provider._messages = long_history()
    records, counted = [], []
    provider.bind_usage("local", records.append)

    def counter(payload, *, deadline):
        counted.append(payload)
        return RequestContextCount(len(str(payload)) // 4, "tokenizer")

    provider.request_counter = counter
    def generate(url, payload, *args, **kwargs):
        return reply("Goal: Keep API; Next: verify" if "Source:" in str(payload) else "done")

    with patch("codey.providers.api_transport.generate", side_effect=generate):
        provider.send("continue")
    assert len(records) >= 2
    assert all(record.purpose == "compaction" for record in records[:-1])
    assert records[-1].purpose == "conversation"
    assert any("Current project state:" in str(payload) for payload in counted)
    assert provider.context_ledger.events()[-1]["content"] == "done"


def test_background_output_reduction_commits_without_a_model_request():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = [{"role": "user", "content": "task"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "output " * 1000 + "\nStored result: exec-17"},
        {"role": "assistant", "tool_calls": [{"id": "latest", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "latest", "content": "latest observation"},
        {"role": "assistant", "content": "recent"}, {"role": "user", "content": "continue"}]
    provider.set_result_refs(("exec-17",))
    with patch("codey.providers.api_transport.generate", side_effect=AssertionError("no semantic request needed")):
        provider.maintain_context()
        provider.wait_for_maintenance(3)
    assert len(provider._messages[2]["content"]) < 2000
    assert "exec-17" in provider._messages[2]["content"]
    provider.close()


def test_late_summary_cannot_commit_to_identical_history_in_a_new_generation():
    started, finish = threading.Event(), threading.Event()
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = long_history()

    def summary(*_):
        started.set()
        assert finish.wait(3)
        return "Goal: keep API"

    provider.summarize_context = summary
    provider.maintain_context()
    assert started.wait(2)
    provider.new_chat()
    provider._messages = long_history()
    finish.set()
    provider.wait_for_maintenance(3)
    assert provider._messages == long_history()


def test_commit_failure_keeps_original_live_history(tmp_path, monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider.bind_context("a", tmp_path)
    provider._messages = long_history()
    original = list(provider._messages)
    provider.summarize_context = lambda *_: "Goal: keep API"
    monkeypatch.setattr(provider.context_ledger, "commit", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert provider._messages == original


def test_incremental_compaction_keeps_prior_checkpoints_and_only_summarizes_new_work():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = long_history()
    seen = []
    provider.summarize_context = lambda source, _: (seen.append(source) or "Goal: keep API")
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    provider._messages.extend([{"role": "assistant", "content": "new info " * 500}, {"role": "user", "content": "new correction"}])
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert len(seen) == 2
    assert "step 0:" not in str(seen[1])
    assert "new info" in str(seen[1])
    assert "Current project state:" not in str(seen[1])
    provider._messages.extend([{"role": "assistant", "content": "third info " * 500}, {"role": "user", "content": "third correction"}])
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert len(seen) == 3
    assert "step 0:" not in str(seen[2])
    assert "new info" not in str(seen[2])
    assert "third info" in str(seen[2])
    assert sum("Current project state:" in str(item) for item in provider._messages) == 3


def test_background_can_commit_older_work_while_the_latest_tool_is_unanswered():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = [*long_history(), {"role": "assistant", "tool_calls": [
        {"id": "pending", "function": {"name": "run", "arguments": "{}"}}]}]
    provider.summarize_context = lambda *_: "Goal: keep API"
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert any("Current project state:" in str(item) for item in provider._messages)
    assert provider._messages[-1]["tool_calls"][0]["id"] == "pending"


def test_auxiliary_created_before_close_cannot_start_a_late_request():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    auxiliary = provider.fork_auxiliary()
    provider.close()
    with patch("codey.providers.api_transport.generate", return_value=reply()) as generated, pytest.raises(RuntimeError, match="cancelled"):
        auxiliary.send("late source")
    generated.assert_not_called()


def test_identical_summary_answers_keep_distinct_original_source_lineage():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = long_history()
    provider.summarize_context = lambda *_: "Goal: keep API"
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    first = next(item for item in provider._messages if "Current project state:" in str(item))
    provider._messages.extend([{"role": "assistant", "content": "new info " * 500}, {"role": "user", "content": "new correction"}])
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    checkpoints = [item for item in provider._messages if "Current project state:" in str(item)]
    assert len(checkpoints) == 2
    assert "step 0:" in str(provider.context_ledger.original_source([first]))
    assert "new info" not in str(provider.context_ledger.original_source([first]))


def test_checkpoint_only_pressure_merges_a_bounded_pair_from_original_sources():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = long_history()
    seen = []
    provider.summarize_context = lambda source, _: (seen.append(source) or "Goal: keep API. " * 20)
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    provider._messages.extend([{"role": "assistant", "content": "new info " * 500}, {"role": "user", "content": "new correction"}])
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    checkpoints = [item for item in provider._messages if "Current project state:" in str(item)]
    provider._messages = [*checkpoints, {"role": "user", "content": "latest"}]
    provider.summarize_context = lambda source, _: (seen.append(source) or "Goal: Keep API. New info.")
    from codey.providers.token_accounting import ContextBudget
    provider._context_budget = ContextBudget(300, 50, 0, 100)
    with patch("codey.providers.api_transport.generate", return_value=reply()):
        provider.send("continue")
    assert len(seen) == 3
    assert "step 0:" in str(seen[-1]) and "new info" in str(seen[-1])
    assert sum("Current project state:" in str(item) for item in provider._messages) == 1


def test_a_large_accepted_answer_schedules_maintenance_before_the_next_request(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=1000)
    scheduled = []
    monkeypatch.setattr(provider, "maintain_context", lambda: scheduled.append(True))
    with patch("codey.providers.api_transport.generate", return_value=reply("large observation " * 1500)):
        provider.send("short question")
    assert scheduled == [True]


def test_maintenance_waits_for_eighty_percent_instead_of_summarizing_normal_growth(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=1000)
    scheduled = []
    monkeypatch.setattr(provider, "maintain_context", lambda: scheduled.append(True))
    provider.request_counter = lambda *args, **kwargs: RequestContextCount(2000, "tokenizer")
    with patch("codey.providers.api_transport.generate", return_value=reply()):
        provider.send("continue normal work")
    assert scheduled == []
    provider.request_counter = lambda *args, **kwargs: RequestContextCount(2600, "tokenizer")
    with patch("codey.providers.api_transport.generate", return_value=reply()):
        provider.send("continue near budget")
    assert scheduled == [True]


def test_recent_history_target_cannot_override_a_smaller_independent_input_limit():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=32768,
                           context_reserve_tokens=1024, context_keep_recent_tokens=12000,
                           input_limit_tokens=2048)
    provider._messages = long_history()
    provider.summarize_context = lambda *_: "Goal: Keep API. Latest constraint: keep files local."
    with patch("codey.providers.api_transport.generate", return_value=reply()):
        assert provider.send("continue") == "ok"
    assert provider.last_context_count.value <= 2048
    assert any("Current project state:" in str(item) for item in provider._messages)


def test_background_thread_start_failure_does_not_invalidate_an_accepted_answer(monkeypatch):
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=1000)
    provider._messages = [{"role":"assistant", "content":"old " * 1600}]
    provider.request_counter = lambda *args, **kwargs: RequestContextCount(2000, "tokenizer")
    monkeypatch.setattr(provider, "maintain_context", lambda: (_ for _ in ()).throw(RuntimeError("cannot start thread")))
    with patch("codey.providers.api_transport.generate", return_value=reply("accepted")):
        assert provider.send("continue") == "accepted"
    assert provider.context_ledger.events()[-1]["content"] == "accepted"
