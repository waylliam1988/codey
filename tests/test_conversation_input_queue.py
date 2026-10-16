from codey.app.input_queue import ConversationInputQueue


def request(text="supplement"):
    return {"session_id": "chat", "run_id": "run_queued", "task": text, "provider": "local"}


def test_queued_input_is_durable_editable_and_submitted_once(tmp_path):
    calls = []
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    item = queue.create("input-one", request())
    assert queue.create("input-one", request()) == item
    assert queue.dispatch_one(lambda: False, lambda body: calls.append(body)) is False
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    queue.edit("input-one", item["revision"], "updated")
    assert queue.dispatch_one(lambda: True, lambda body: (200, {"ok": True, "run_id": body["run_id"]}))
    assert queue.get("input-one")["state"] == "submitted"
    assert queue.dispatch_one(lambda: True, lambda body: calls.append(body)) is False
    assert calls == []
    assert queue.create("input-one", request())["state"] == "submitted"


def test_uncertain_submission_is_never_automatically_replayed_after_restart(tmp_path):
    calls = []
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    queue.create("input-one", request())

    def lost_reply(body):
        calls.append(body)
        raise TimeoutError("response lost after admission")

    queue.dispatch_one(lambda: True, lost_reply)
    assert queue.get("input-one")["state"] == "unconfirmed"
    restarted = ConversationInputQueue(tmp_path, emit=lambda event: None)
    assert restarted.dispatch_one(lambda: True, lost_reply) is False
    assert len(calls) == 1


def test_restart_keeps_an_admitted_input_visible_when_its_final_result_is_unknown(tmp_path):
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    queue.create("input-one", request())
    queue.dispatch_one(lambda: True, lambda body: (200, {"ok": True}))
    queue.close()
    restored = ConversationInputQueue(tmp_path, emit=lambda event: None)
    assert restored.get("input-one")["state"] == "unconfirmed"
    assert restored.dispatch_one(lambda: True, lambda body: (200, {"ok": True})) is False
    restored.close()


def test_server_failure_after_submission_is_not_treated_as_confirmed_non_delivery(tmp_path):
    import pytest

    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    queue.create("input-one", request())
    queue.dispatch_one(lambda: True, lambda body: (500, {"error": "outcome unknown"}))
    item = queue.get("input-one")
    assert item["state"] == "unconfirmed"
    with pytest.raises(ValueError, match="already"):
        queue.remove(item["id"], item["revision"])
    assert queue.dispatch_one(lambda: True, lambda body: (200, {"ok": True})) is False
    queue.close()


def test_stop_pauses_queued_inputs_and_remove_requires_the_current_revision(tmp_path):
    import pytest

    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    item = queue.create("input-one", request())
    queue.pause()
    assert queue.get("input-one")["state"] == "paused"
    with pytest.raises(ValueError, match="revision"):
        queue.remove("input-one", item["revision"])
    current = queue.get("input-one")
    queue.remove("input-one", current["revision"])
    assert queue.get("input-one")["state"] == "cancelled"


def test_invalid_edit_does_not_change_the_accepted_item(tmp_path):
    import pytest

    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    item = queue.create("input-one", request())
    with pytest.raises(ValueError):
        queue.edit("input-one", item["revision"], " ")
    assert queue.get("input-one") == item


def test_terminal_event_can_win_the_race_with_the_submit_response(tmp_path):
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    queue.create("input-one", request())

    def fast_finish(body):
        queue.completed(body["run_id"], {"type": "task_done", "run_id": body["run_id"], "summary": "done"})
        return 200, {"ok": True}

    queue.dispatch_one(lambda: True, fast_finish)
    assert queue.get("input-one")["state"] == "completed"


def test_completed_receipts_do_not_permanently_fill_the_pending_queue(tmp_path):
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    for index in range(260):
        body = {**request(), "run_id": f"run_{index}"}
        queue.create(f"input-{index}", body)
        queue.dispatch_one(lambda: True, lambda body: (200, {"ok": True}))
        queue.completed(body["run_id"], {"type": "task_done", "run_id": body["run_id"], "summary": "done"})
    restored = ConversationInputQueue(tmp_path, emit=lambda event: None)
    assert restored.create("input-0", {**request(), "run_id": "run_0"})["state"] == "completed"


def test_fast_completion_can_evict_its_old_cache_entry_before_submit_returns(tmp_path):
    queue = ConversationInputQueue(tmp_path, emit=lambda event: None)
    queue.create("old", request())
    for index in range(130):
        identity = f"cancel-{index}"
        item = queue.create(identity, {**request(), "run_id": f"r{index}"})
        queue.remove(identity, item["revision"])

    def finish(body):
        queue.completed(body["run_id"], {"type": "task_done", "run_id": body["run_id"]})
        return 200, {"ok": True}

    assert queue.dispatch_one(lambda: True, finish)
    assert queue.get("old")["state"] == "completed"
    queue.close()
