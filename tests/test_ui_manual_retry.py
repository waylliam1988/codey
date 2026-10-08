"""Manual retries belong to one request, even across failures and restoration."""
from __future__ import annotations

import re
from unittest import mock

import pytest
from playwright.sync_api import expect

from codey.app.api import run_submit_response
from codey.storage.ui_state_store import UiStateStore
from tests import test_ui_workflow as workflow

page = workflow.page
ui_browser = workflow.ui_browser


def backend(page):
    calls = []
    state = {"busy": False}

    def submit(route):
        body = route.request.post_data_json
        calls.append(body)
        run_id = body.get("run_id") or f"old-run-{len(calls)}"
        state.update(busy=True, run_id=run_id, session_id=body["session_id"])
        route.fulfill(json={"ok": True, "run_id": run_id})

    page.route("**/api/run", submit)
    page.route("**/api/state", lambda route: route.fulfill(json=state))
    return calls, state


def send(page, calls, text="你好"):
    page.locator("#task").fill(text)
    page.locator("#send").click()
    page.wait_for_function("CodeyComposer.isSending() === false")
    return calls[-1].get("run_id") or f"old-run-{len(calls)}"


def finish(page, state, run_id, summary="ERROR: model HTTP 503: backend temporarily overloaded", reason="error"):
    event = {"type": "task_done", "session_id": "a", "run_id": run_id,
             "summary": summary, "stop_reason": reason, "mode": "chat"}
    state.clear()
    state.update(busy=False, last_terminal_event=event)
    page.evaluate("handleServerEvent", event)


def retry(page):
    page.get_by_role("button", name="Retry", exact=True).click()
    page.wait_for_function("CodeyComposer.isSending() === false")


def test_retry_updates_one_message_and_error_then_recovers(page):
    calls, state = backend(page)
    first = send(page, calls)
    finish(page, state, first)
    page.evaluate("window.originalUser = document.querySelector('.msg.user')")
    page.locator("#task").fill("保留这个新草稿")
    retry(page)
    expect(page.locator(".msg.user")).to_have_count(1)
    expect(page.locator(".request-status .status-row.err")).to_have_count(0)
    expect(page.locator(".request-status")).to_contain_text("Retrying")
    assert calls[1]["task"] == "你好"
    assert calls[1]["run_id"] != first
    finish(page, state, calls[1]["run_id"], "ERROR: model HTTP 403: Free tier only available in OpenCode")
    expect(page.locator(".status-row.err")).to_have_count(1)
    page.get_by_role("button", name="Details", exact=True).click()
    expect(page.locator(".retry-details")).to_contain_text("503")
    expect(page.locator(".retry-details")).to_contain_text("403")
    retry(page)
    finish(page, state, calls[2]["run_id"], "你好！", "done")
    expect(page.locator(".status-row.err")).to_have_count(0)
    expect(page.locator(".msg.asst")).to_have_count(1)
    expect(page.locator("#task")).to_have_value("保留这个新草稿")
    assert page.evaluate("originalUser === document.querySelector('.msg.user')")


def test_old_error_cannot_resend_a_newer_question(page):
    calls, state = backend(page)
    first = send(page, calls, "first question")
    finish(page, state, first)
    second = send(page, calls, "second question")
    finish(page, state, second)
    old_retry = page.get_by_role("button", name="Retry", exact=True).first
    expect(old_retry).to_be_disabled()
    old_retry.evaluate("e => e.click()")
    assert len(calls) == 2


def test_late_attempt_events_do_not_replace_the_current_status(page):
    calls, state = backend(page)
    first = send(page, calls)
    finish(page, state, first)
    retry(page)
    second = calls[-1].get("run_id") or "old-run-2"
    for event_type in ("task_start", "ghost_post_turn_warning", "task_done"):
        page.evaluate("handleServerEvent", {"type": event_type, "session_id": "a", "run_id": first,
                                           "stop_reason": "error", "summary": "ERROR: stale failure"})
    expect(page.locator(".status-row.err")).to_have_count(0)
    assert page.evaluate("runningRunId") == second
    finish(page, state, second, "Recovered", "done")
    expect(page.locator(".msg.asst")).to_contain_text("Recovered")


def test_warning_is_attempt_detail_and_tools_survive_retry(page):
    calls, state = backend(page)
    first = send(page, calls)
    page.evaluate("handleServerEvent", {"type": "tool", "kind": "read", "path": "keep.py", "result": "read",
                                       "session_id": "a", "run_id": first, "tool_id": "t1"})
    page.evaluate("handleServerEvent", {"type": "ghost_post_turn_warning", "session_id": "a", "run_id": first})
    finish(page, state, first)
    assert "Local update paused" not in page.locator("#chat").inner_text()
    page.get_by_role("button", name="Details", exact=True).click()
    expect(page.locator(".retry-details")).to_contain_text("Local update paused")
    page.evaluate("window.originalProcess = document.querySelector('.process-block')")
    retry(page)
    expect(page.locator(".process-block")).to_have_count(1)
    assert page.evaluate("originalProcess === document.querySelector('.process-block')")
    assert page.evaluate("document.querySelector('.process-block').textContent.includes('keep.py')")


def test_attempt_identity_is_known_before_the_http_response(page):
    calls, state = backend(page)
    run_id = send(page, calls)
    assert re.fullmatch(r"run_[a-f0-9]{32}", run_id)
    request = page.evaluate("CodeyUiState.current().sessions.find(s => s.id === 'a').messages.find(m => m.type === 'user')")
    assert request["id"]
    assert request["attempts"][0]["runId"] == run_id


def test_attempt_recovery_fields_survive_storage(tmp_path):
    store = UiStateStore(tmp_path)
    attempt = {"runId": "run_" + "a" * 32, "state": "failed", "error": "Select the model again",
               "sendFailure": "model_selection", "runOwner": "b", "warning": "Local update paused"}
    messages = [{"type": "user", "id": "message-1", "text": "original", "attempts": [attempt]},
                {"type": "request_status", "requestId": "message-1", "sessionId": "a"}]
    store.save({"active_id": "a", "sessions": [{"id": "a", "messages": messages}], "projects": []}, base_revision=0)
    assert store.load()["sessions"][0]["messages"] == messages


def test_run_admission_preserves_the_client_attempt_identity():
    calls = []
    run_id = "run_" + "a" * 32

    def submit(*args, **kwargs):
        calls.append(kwargs)
        return kwargs.get("run_id", "server-generated")

    status, data = run_submit_response({"task": "hello", "run_id": run_id}, submit)
    assert status == 200
    assert data["run_id"] == run_id
    assert calls[0]["run_id"] == run_id


def test_malformed_attempt_identity_is_rejected_before_admission():
    calls = []
    status, _ = run_submit_response({"task": "hello", "run_id": "../unsafe"}, lambda *a, **k: calls.append(k))
    assert status == 400
    assert calls == []


def test_admission_reserves_the_same_attempt_for_the_worker():
    from codey.app import task_submit
    from tests.test_task_submit import _fake_reserved, _FakeState

    state = _FakeState()
    run_id = "run_" + "b" * 32
    with (mock.patch.object(state, "reserve_run", return_value=_fake_reserved(run_id)) as reserve,
          mock.patch.object(task_submit, "submit_browser_task", return_value=True) as worker):
        assert task_submit.submit_task("a", None, "hello", 8, False, "deepseek",
                                       run_id=run_id, get_state=lambda: state) == run_id
    assert reserve.call_args.kwargs["run_id"] == run_id
    assert worker.call_args.args[8] == run_id


def test_unlinked_error_never_guesses_a_question_to_retry(page):
    page.evaluate("addToSession('a', {type:'err', text:'Independent operation failed', sessionId:'a'})")
    expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)


def test_accepted_attempt_missing_from_authoritative_state_does_not_spin_forever(page):
    page.route("**/api/run", lambda route: route.fulfill(json={"run_id": route.request.post_data_json["run_id"]}))
    page.route("**/api/state", lambda route: route.fulfill(json={"busy": False}))
    page.locator("#task").fill("hello")
    page.locator("#send").click()
    page.wait_for_function("!CodeyComposer.isSending()")
    expect(page.locator(".request-status")).to_contain_text("Response was not confirmed")
    expect(page.get_by_role("button", name="Retry", exact=True)).to_be_enabled()


def test_terminal_event_before_ack_is_not_revived_and_double_retry_is_blocked(page):
    calls, state = backend(page)
    first = send(page, calls)
    finish(page, state, first)
    page.evaluate("""() => {
        const real = fetch;
        window.pendingCalls = [];
        window.fetch = (url, options) => url === '/api/run' ? new Promise(resolve => {
            const body = JSON.parse(options.body); pendingCalls.push(body);
            window.acceptPending = () => resolve(new Response(JSON.stringify({ok:true,run_id:body.run_id})));
        }) : real(url, options);
        const button = document.querySelector('.request-status button:last-child');
        button.click(); button.click();
    }""")
    assert page.evaluate("pendingCalls.length") == 1
    second = page.evaluate("pendingCalls[0].run_id")
    finish(page, state, second, "Immediate reply", "done")
    page.evaluate("acceptPending()")
    page.wait_for_function("!CodeyComposer.isSending()")
    assert page.evaluate("runningRunId") is None
    expect(page.locator(".msg.asst")).to_have_count(1)
    expect(page.locator(".msg.user")).to_have_count(1)


@pytest.mark.parametrize("restored", ["failed", "running", "missing"])
def test_restored_request_recovers_in_place(page, tmp_path, restored):
    run_id = "run_" + "c" * 32
    messages = [{"type": "user", "id": "restored", "text": "original", "attempts": [
        {"runId": run_id, "state": "failed" if restored == "failed" else "running", "error": "Original failure"}]},
        {"type": "request_status", "sessionId": "a", "requestId": "restored"}]
    store = UiStateStore(tmp_path)
    store.save({"active_id": "a", "sessions": [{"id": "a", "provider": "deepseek", "messages": messages}], "projects": []}, base_revision=0)
    saved = store.load()
    page.route("**/api/ui_state", lambda route: route.fulfill(json={"ok": True, "state": saved}))
    calls, state = backend(page)
    state.update(busy=restored == "running", run_id=run_id, session_id="a")
    page.reload()
    page.wait_for_function("CodeyUiState.current().sessions[0].messages.length === 2")
    page.evaluate("CodeySse.reconcileRunState()")
    expect(page.locator(".msg.user")).to_have_count(1)
    if restored == "running":
        expect(page.locator(".request-status")).to_contain_text("Waiting for reply")
        finish(page, state, run_id)
    else:
        expect(page.locator(".request-status")).to_contain_text("Original failure" if restored == "failed" else "Response was not confirmed")
    retry(page)
    assert calls[0]["task"] == "original"
    expect(page.locator(".msg.user")).to_have_count(1)


def test_model_recovery_still_allows_explicit_retry_without_sending_automatically(page):
    calls = []

    def reject(route):
        calls.append(route.request.post_data_json)
        route.fulfill(status=400, json={"reason": "model_selection_invalid"})

    page.route("**/api/run", reject)
    send(page, calls, "original")
    page.get_by_role("button", name="Choose model", exact=True).last.click()
    assert len(calls) == 1
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name="Retry", exact=True)).to_be_enabled()


def test_attempt_details_use_the_available_width_on_mobile(page):
    calls, state = backend(page)
    first = send(page, calls)
    finish(page, state, first)
    page.set_viewport_size({"width": 320, "height": 800})
    page.get_by_role("button", name="Details", exact=True).click()
    label = page.locator(".retry-details .run-details-label").bounding_box()
    value = page.locator(".retry-details .run-details-value").bounding_box()
    assert value["x"] == label["x"]
    assert value["width"] >= 250


def test_pending_approval_restores_after_its_run_has_paused(page):
    calls, state = backend(page)
    run_id = send(page, calls)
    finish(page, state, run_id, "", "approval")
    page.evaluate("applyRunState", {"busy": False, "last_terminal_event": state["last_terminal_event"],
                                   "pending_event": {"type": "shell_request", "id": "approve-1",
                                                     "session_id": "a", "run_id": run_id,
                                                     "command": "pytest -q", "cwd": "E:/demo"}})
    expect(page.locator(".shell-command")).to_contain_text("pytest -q")
    expect(page.locator(".request-status")).to_be_hidden()


def test_current_attempt_can_receive_its_answer_after_terminal_state(page):
    calls, state = backend(page)
    run_id = send(page, calls)
    finish(page, state, run_id, "", "done")
    page.evaluate("handleServerEvent", {"type": "reply", "session_id": "a", "run_id": run_id, "text": "Final answer"})
    expect(page.locator(".msg.asst")).to_contain_text("Final answer")


def test_prior_attempt_still_releases_global_busy_state_after_retry_admission_refusal(page):
    calls, state = backend(page)
    first = send(page, calls)
    finish(page, state, first)
    state.clear()
    state.update(busy=True, run_id=first, session_id="a")
    page.route("**/api/run", lambda route: route.fulfill(status=409, json={"error": "busy"}))
    retry(page)
    assert page.evaluate("runningRunId") == first
    finish(page, state, first, "Earlier execution finished", "done")
    assert page.evaluate("runningRunId") is None
    expect(page.get_by_role("button", name="Retry", exact=True)).to_be_enabled()


def test_continuation_model_recovery_also_survives_storage(tmp_path):
    store = UiStateStore(tmp_path)
    message = {"type": "err", "text": "Select the model again", "sessionId": "a", "sendFailure": "model_selection", "runOwner": ""}
    store.save({"active_id": "a", "sessions": [{"id": "a", "messages": [message]}], "projects": []}, base_revision=0)
    assert store.load()["sessions"][0]["messages"] == [message]
