from unittest import mock

import pytest
from playwright.sync_api import expect

from codey.app import api, task_submit
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def seed(page):
    ctx = page.codey_context
    durable = ctx.durable
    for active in ctx.run_registry.active_runs():
        ctx.release_run(active.run_id)
    if durable.has_conversation("a"):
        for task in durable.pending_tasks():
            if task.conversation_id == "a":
                durable.cancel_task(task.task_id)
        durable.delete_conversation("a")
    sources = []
    for name in ("first", "second"):
        durable.admit_input(conversation_id="a", request_id=name, task_id=name, text=f"Question {name}", provider_snapshot={})
        sources.append(durable.task(name).state["input_entry_id"])
        durable.record_display_message("a", run_id=name, identity="tool", payload={
            "type": "tool", "kind": "read", "path": f"{name}.py", "result": "old work", "runId": name})
        durable.complete_task(name, f"Answer {name}", state={})
    page.evaluate("() => ConversationUI.refreshHistory('a')")
    page.evaluate("""() => {
      addToSession('a',{type:'run_state',runId:'second',state:'paused'});
      addToSession('a',{type:'pause',runId:'second',text:'Old pause',sourceRunId:'second'});
      document.getElementById('task').value='Separate composer draft';
      Composer.captureDraft();
    }""")
    return sources


@pytest.mark.parametrize("position", [0, 1])
def test_edit_replaces_old_work_and_suffix_and_rejects_late_events(page, position):
    sources = seed(page)
    ctx = page.codey_context
    bodies = []
    def submit(route):
        body = route.request.post_data_json
        bodies.append(body)
        with mock.patch.object(task_submit, "submit_browser_task", return_value=True):
            status, result = api.run_submit_response(body, ctx=ctx,
                submit_task=lambda *args, **kwargs: task_submit.submit_task(*args, **kwargs, get_state=lambda: ctx))
        route.fulfill(status=status, json=result)
    page.route("**/api/run", submit)
    page.locator(".msg.user .msg-edit").nth(position).click(force=True)
    page.locator(".msg-edit-input").fill("Replacement question")
    page.locator(".msg-edit-send").evaluate("button=>{button.click();button.click()}")
    expect(page.locator(".msg-edit-editor")).to_have_count(0)
    expect(page.locator(".msg.user")).to_have_count(position + 1)
    expect(page.locator("#chat")).not_to_contain_text("Answer second")
    expect(page.locator("#chat")).not_to_contain_text("second.py")
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    assert len(bodies) == 1
    assert bodies[0]["input"]["entry_id"] == sources[position]
    assert bodies[0]["input"]["kind"] == "edit_user_message"
    new_run = bodies[0]["run_id"]
    page.evaluate("""run=>{
      handleServerEvent({type:'task_start',session_id:'a',run_id:run,task_revision:2,task_status:'running'});
      for(const type of ['reply','reply_stream','shell_result','task_done'])
        handleServerEvent({type,session_id:'a',run_id:'second',id:'late-approval',command:'OLD COMMAND',
          text:'LATE OLD ANSWER',delta:'LATE OLD ANSWER',stream_id:'late',sequence:1,generation:1,
          task_revision:100,task_status:'cancelled',stop_reason:'stopped'});
    }""", new_run)
    page.wait_for_timeout(100)  # Flush the animation-frame stream coalescer.
    expect(page.locator("#chat")).not_to_contain_text("LATE OLD ANSWER")
    expect(page.locator("#chat")).not_to_contain_text("OLD COMMAND")
    expect(page.locator("#stop")).to_be_visible()
    expect(page.locator("#task")).to_have_value("Separate composer draft")
    page.evaluate("() => ConversationUI.refreshHistory('a')")
    expect(page.locator(".msg.user")).to_have_count(position + 1)
    expect(page.locator("#chat")).not_to_contain_text("Old pause")


def test_edit_conflict_keeps_inline_draft_and_original_history(page):
    seed(page)
    page.route("**/api/run", lambda route: route.fulfill(status=409, json={"error": "Conversation changed. Cancel the edit and try again."}))
    page.locator(".msg.user .msg-edit").first.click(force=True)
    page.locator(".msg-edit-input").fill("Keep my edited draft")
    page.locator(".msg-edit-send").click()
    expect(page.locator(".msg-edit-error")).to_contain_text("Conversation changed")
    expect(page.locator(".msg-edit-input")).to_have_value("Keep my edited draft")
    expect(page.locator(".msg-edit-input")).to_be_enabled()
    expect(page.locator("#chat")).to_contain_text("Answer second")
    expect(page.locator(".msg.user")).to_have_count(2)


def test_edit_sending_remains_stoppable_without_appending_another_user_message(page):
    seed(page)
    pending, stops = [], []
    page.route("**/api/run", lambda route: pending.append(route))
    def stop(route):
        stops.append(route.request.post_data_json["run_id"])
        route.fulfill(json={"ok": True})
    page.route("**/api/stop", stop)
    page.locator(".msg.user .msg-edit").first.click(force=True)
    page.locator(".msg-edit-input").fill("Slow edit")
    page.locator(".msg-edit-send").click()
    expect(page.locator("#stop")).to_be_visible()
    expect(page.locator(".msg.user")).to_have_count(2)
    page.locator("#stop").click()
    page.wait_for_function("!Composer.isSending()")
    assert len(pending) == 1 and stops == [pending[0].request.post_data_json["run_id"]]
    expect(page.locator(".msg-edit-input")).to_have_value("Slow edit")
    expect(page.locator(".msg-edit-input")).to_be_enabled()


@pytest.mark.parametrize("target_busy", [False, True])
def test_edit_uses_target_chat_ownership_when_another_chat_is_primary(page, target_busy):
    seed(page)
    page.evaluate("""targetBusy => {
      if(targetBusy) handleServerEvent({type:'task_start',session_id:'a',run_id:'active-a',task_revision:1,task_status:'running'});
      handleServerEvent({type:'task_start',session_id:'b',run_id:'active-b',task_revision:1,task_status:'running'});
    }""", target_busy)
    page.locator(".msg.user .msg-edit").first.click(force=True)
    expect(page.locator(".msg-edit-input")).to_have_count(0 if target_busy else 1)
    if not target_busy:
        expect(page.locator(".msg-edit-input")).to_be_enabled()
        page.get_by_role("button", name="Cancel", exact=True).click()
        expect(page.locator(".msg-edit-input")).to_have_count(0)
