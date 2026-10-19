from __future__ import annotations

import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from codey.app import api, shell_service
from codey.app.context import AppContext
from tests.support.shell_approval import seed_requested_approval as seed


def test_accept_returns_before_executor_finishes_and_is_idempotent(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    release = threading.Event()
    entered = threading.Event()
    returned = threading.Event()
    replies = []

    def execute(*args):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "status": "exit", "output": "done", "exit_code": 0}

    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    submit = Mock(return_value="resume")

    def request():
        replies.append(
            api.shell_approval_response(
                ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=submit
            )
        )
        returned.set()

    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert entered.wait(5)
        assert returned.wait(0.5), "approval HTTP response waits for shell execution"
        assert replies[0][0] == 202
        code, same = api.shell_approval_response(
            ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=submit
        )
        assert code == 202 and same["decision"] == "allow"
        code, conflict = api.shell_approval_response(
            ctx, {"id": "approval", "approved": False}, submit_task_after_slot_release=submit
        )
        assert code == 409 and conflict["code"] == "decision_conflict"
    finally:
        release.set()
        thread.join(5)
        assert ctx.shell_approval_workflow.wait(5)
        ctx.close()


def test_result_is_durable_before_publication(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx, continue_after=False)
    monkeypatch.setattr(
        shell_service,
        "execute_shell_ticket",
        lambda *a: {"ok": True, "status": "exit", "output": "done", "exit_code": 0},
    )
    original = ctx.record_shell_result
    observed = []

    def publish(event):
        observed.append(ctx.resolve_shell_approval(event["id"]))
        original(event)

    monkeypatch.setattr(ctx, "record_shell_result", publish)
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=Mock())
        assert ctx.shell_approval_workflow.wait(5)
        assert observed and observed[0] is not None, "result published before recoverable settlement"
    finally:
        ctx.close()


def test_failed_decision_write_preserves_pending_card(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    fact = seed(ctx)
    from codey.app.shell_approval_state import restore_pending_shell_approvals

    restore_pending_shell_approvals(ctx)
    monkeypatch.setattr(ctx.durable, "transition_approval", Mock(side_effect=OSError("durable unavailable")))
    try:
        code, payload = api.shell_approval_response(
            ctx, {"id": fact.approval_id, "approved": False}, submit_task_after_slot_release=Mock()
        )
        assert code == 409 and payload["code"] == "decision_conflict"
        assert fact.approval_id in ctx.pending_shell_approvals(), "failed persistence consumed approval card"
    finally:
        monkeypatch.undo()
        ctx.close()


def test_failed_expiry_write_preserves_pending_card(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    from codey.app.shell_approval_state import restore_pending_shell_approvals

    restore_pending_shell_approvals(ctx)
    try:
        with monkeypatch.context() as failing_store:
            failing_store.setattr(ctx.durable, "transition_approval", Mock(side_effect=OSError("durable unavailable")))
            with pytest.raises(OSError, match="durable unavailable"):
                ctx.expire_pending_shell_approvals()
        assert "approval" in ctx.pending_shell_approvals(), "expiry removed the card before committing its verdict"
        assert ctx.shell_approval_records.latest("approval").kind == "requested"
    finally:
        ctx.close()


def test_failed_stop_write_still_invalidates_claimed_execution_ticket(tmp_path, monkeypatch):
    from codey.app.shell_approval_state import restore_pending_shell_approvals

    ctx = AppContext(tmp_path)
    claimed_fact = seed(ctx, "claimed")
    seed(ctx, "pending")
    ctx.record_shell_approval_fact(replace(claimed_fact, kind="approved"))
    restore_pending_shell_approvals(ctx)
    _, ticket = shell_service.claim_shell_ticket(ctx, "claimed", timeout=5, output_limit=1024)
    assert ticket is not None
    try:
        with monkeypatch.context() as failing_store:
            failing_store.setattr(ctx.durable, "transition_approval", Mock(side_effect=OSError("durable unavailable")))
            with pytest.raises(OSError, match="durable unavailable"):
                ctx.request_stop()
        # New work may clear the transient flag; the old ticket must stay invalid.
        ctx.run_registry.stop_flag.clear()
        result = shell_service.execute_shell_ticket(ctx, ticket)
        assert result["status"] == "stopped", "failed Stop persistence left a claimed command executable"
        assert "pending" in ctx.pending_shell_approvals()
    finally:
        ctx.close()


def test_restart_accepted_allow_executes_but_claimed_allow_never_reexecutes(tmp_path, monkeypatch):
    first = AppContext(tmp_path)
    accepted = seed(first, "accepted", continue_after=False)
    claimed = seed(first, "claimed", continue_after=False)
    first.record_shell_approval_fact(replace(accepted, kind="approved"))
    first.record_shell_approval_fact(replace(claimed, kind="approved"))
    first.record_shell_approval_fact(replace(claimed, kind="execution_claimed"))
    first.close()
    ctx = AppContext(tmp_path)
    execute = Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0})
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    try:
        ctx.shell_approval_workflow.start(Mock())
        assert ctx.shell_approval_workflow.wait(5)
        assert execute.call_count == 1
        assert ctx.shell_approval_workflow.status("accepted")["execution"] == "settled"
        assert ctx.shell_approval_workflow.status("claimed")["execution"] == "uncertain"
    finally:
        ctx.close()


def test_stop_cancels_accepted_queue_across_restart(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx, continue_after=False)
    monkeypatch.setattr(ctx.shell_approval_workflow, "_schedule", lambda _: None)
    api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=Mock())
    ctx.request_stop()
    ctx.close()
    restarted = AppContext(tmp_path)
    execute = Mock()
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    try:
        restarted.shell_approval_workflow.start(Mock())
        assert restarted.shell_approval_workflow.wait(5)
        execute.assert_not_called()
        assert restarted.shell_approval_workflow.status("approval")["decision"] == "cancelled"
    finally:
        restarted.close()


def test_shutdown_preserves_undecided_approval(tmp_path):
    ctx = AppContext(tmp_path)
    seed(ctx)
    from codey.app.shell_approval_state import restore_pending_shell_approvals

    restore_pending_shell_approvals(ctx)
    ctx.close()
    restarted = AppContext(tmp_path)
    try:
        assert restarted.run_state_payload()["pending_event"]["id"] == "approval"
    finally:
        restarted.close()


def test_continuation_retry_never_reexecutes_settled_command(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    execute = Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0})
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    submit = Mock()
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=submit)
        assert ctx.shell_approval_workflow.wait(5)
        assert ctx.shell_approval_workflow.status("approval")["continuation"] == "started"
        assert ctx.shell_approval_workflow.retry("approval", submit)[0] == 409
        assert execute.call_count == 1
        submit.assert_not_called()
        assert ctx.durable.task("source").status.value in {"pending", "running"}
        assert ctx.durable.approval("approval").continuation_run_id == "source"
    finally:
        ctx.close()


def test_unpersisted_result_is_uncertain_and_not_published(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    monkeypatch.setattr(
        shell_service,
        "execute_shell_ticket",
        Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0}),
    )
    publish = Mock()
    monkeypatch.setattr(ctx, "record_shell_result", publish)
    monkeypatch.setattr(ctx.managed_outputs, "write_json_artifact", Mock(side_effect=OSError("disk full")))
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=Mock())
        assert ctx.shell_approval_workflow.wait(5)
        publish.assert_not_called()
        assert ctx.shell_approval_workflow.status("approval")["execution"] == "uncertain"
        assert ctx.shell_approval_workflow.retry("approval", Mock())[0] == 409
    finally:
        ctx.close()


def test_failed_result_write_does_not_block_next_approval(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    execute = Mock(
        return_value={
            "ok": True,
            "status": "exit",
            "output": "done",
            "exit_code": 0,
        }
    )
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    submit = Mock()

    try:
        seed(ctx, "first", continue_after=False, command="echo same")

        with monkeypatch.context() as failing_disk:
            failing_disk.setattr(
                ctx.managed_outputs,
                "write_json_artifact",
                Mock(side_effect=OSError("disk full")),
            )

            status, _ = api.shell_approval_response(
                ctx,
                {"id": "first", "approved": True},
                submit_task_after_slot_release=submit,
            )

            assert status == 202
            assert ctx.shell_approval_workflow.wait(5)

        first = ctx.shell_approval_workflow.status("first")
        assert first is not None
        assert first["execution"] == "uncertain"
        assert ctx.shell_approval_workflow.busy_status() is None

        seed(ctx, "second", continue_after=False, command="echo same")
        status, _ = api.shell_approval_response(
            ctx,
            {"id": "second", "approved": True},
            submit_task_after_slot_release=submit,
        )

        assert status == 202
        assert ctx.shell_approval_workflow.wait(5)

        second = ctx.shell_approval_workflow.status("second")
        assert second is not None
        assert second["execution"] == "settled"
        assert execute.call_count == 2

        status, _ = ctx.shell_approval_workflow.retry("first", submit)
        assert status == 409
        assert execute.call_count == 2
        submit.assert_not_called()
    finally:
        ctx.close()


def test_stop_during_execution_stays_cancelled_after_result_settlement(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    entered, release = threading.Event(), threading.Event()

    def execute(*_):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "status": "exit", "output": "done", "exit_code": 0}

    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    submit = Mock(return_value="unexpected")
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=submit)
        assert entered.wait(5)
        ctx.request_stop()
        # Starting other work can clear the runtime stop flag. The durable
        # cancellation must still prevent the old approval resuming.
        ctx.run_registry.stop_flag.clear()
        release.set()
        assert ctx.shell_approval_workflow.wait(5)
        submit.assert_not_called()
        assert ctx.shell_approval_workflow.status("approval")["continuation"] == "cancelled"
    finally:
        release.set()
        ctx.close()


def test_resume_entry_blocks_pending_native_approval_before_new_window(tmp_path):
    ctx = AppContext(tmp_path)
    seed(ctx)
    submit = Mock(return_value="unexpected")
    try:
        status, payload = api.run_submit_response(
            {"session_id": "session", "provider": "local", "input": {"kind": "resume_run", "source_run_id": "source"}},
            submit,
            ctx=ctx,
        )
        assert status == 409
        assert payload["code"] == "approval_required"
        submit.assert_not_called()
    finally:
        ctx.close()


def test_restart_preserves_explicit_unattached_chat(tmp_path):
    ctx = AppContext(tmp_path)
    seed(ctx)
    ctx.close()
    restarted = AppContext(tmp_path)
    try:
        assert restarted.pending_shell_approvals()["approval"]["attached_project"] is None
    finally:
        restarted.close()


def test_restart_does_not_resubmit_continuation_when_durable_task_is_unknown(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    execute = Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0})
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    submitted = []

    def enqueue(*args, **kwargs):
        submitted.append(kwargs["run_id"])
        return kwargs["run_id"]

    api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=enqueue)
    assert ctx.shell_approval_workflow.wait(5)
    ctx.close()
    restarted = AppContext(tmp_path)
    try:
        restarted.shell_approval_workflow.start(enqueue)
        assert restarted.shell_approval_workflow.wait(5)
        # Same-task suspension has no second continuation identity to submit.
        assert len(submitted) == 0
        assert execute.call_count == 1
    finally:
        restarted.close()


def test_ui_state_does_not_own_approval_transcript(tmp_path):
    ctx = AppContext(tmp_path)
    fact = seed(ctx)
    state = {
        "type": "shell_approval_status",
        "id": "approval",
        "session_id": "session",
        "decision": "allow",
        "execution": "settled",
        "continuation": "failed",
        "revision": 5,
    }
    try:
        ctx.save_ui_state(
            {
                "active_id": "session",
                "sessions": [
                    {
                        "id": "session",
                        "title": "Approval session",
                        "messages": [{"type": "shell_approval_status", "id": "approval", "approval": state}],
                    }
                ],
            },
            base_revision=0,
        )
        restored = ctx.load_ui_state()
        assert restored["active_id"] == "session"
        assert restored["sessions"][0]["title"] == "Approval session"
        assert "messages" not in restored["sessions"][0]
        latest = ctx.shell_approval_records.latest(fact.approval_id)
        assert latest is not None and latest.approval_id == fact.approval_id
    finally:
        ctx.close()


def test_decision_requires_verified_command_even_with_live_card(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    fact = seed(ctx)
    from codey.app.shell_approval_state import restore_pending_shell_approvals

    restore_pending_shell_approvals(ctx)
    monkeypatch.setattr(ctx.managed_outputs, "read_json_artifact", Mock(side_effect=ValueError("digest mismatch")))
    try:
        code, payload = api.shell_approval_response(
            ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=Mock()
        )
        assert code == 409 and payload["code"] == "command_unavailable"
        assert ctx.shell_approval_records.latest(fact.approval_id).kind == "requested"
    finally:
        monkeypatch.undo()
        ctx.close()


def test_worker_publication_never_holds_coordination_lock(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    entered = threading.Event()
    original = ctx.emit

    def emit(event):
        entered.set()
        original(event)

    monkeypatch.setattr(ctx, "emit", emit)
    monkeypatch.setattr(ctx.shell_approval_workflow, "_process", lambda _: None)
    try:
        # Approval admission holds ctx.lock while reading worker status. A
        # completing worker must not reverse that order during publication.
        with ctx.lock:
            ctx.shell_approval_workflow._schedule("approval")
            assert entered.wait(5)
            acquired = ctx.shell_approval_workflow._condition.acquire(timeout=0.5)
            if acquired:
                ctx.shell_approval_workflow._condition.release()
            assert acquired, "worker holds coordination lock while waiting for context lock"
        assert ctx.shell_approval_workflow.wait(5)
    finally:
        ctx.close()


def test_missing_settled_receipt_is_uncertain_and_not_resubmitted(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx, continue_after=False)
    monkeypatch.setattr(
        shell_service,
        "execute_shell_ticket",
        Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0}),
    )
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=Mock())
        assert ctx.shell_approval_workflow.wait(5)
        assert ctx.shell_approval_workflow.status("approval")["execution"] == "settled"
        monkeypatch.setattr(ctx.managed_outputs, "read_json_artifact", Mock(side_effect=ValueError("digest mismatch")))
        assert ctx.shell_approval_workflow.status("approval")["execution"] == "uncertain"
        assert ctx.shell_approval_workflow.retry("approval", Mock())[0] == 409
    finally:
        ctx.close()


@pytest.mark.parametrize("approved", [True, False])
def test_missing_receipt_blocks_failed_continuation_retry_for_both_decisions(tmp_path, monkeypatch, approved):
    ctx = AppContext(tmp_path)
    seed(ctx)
    execute = Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0})
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    submit = Mock(return_value=None)
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": approved}, submit_task_after_slot_release=submit)
        assert ctx.shell_approval_workflow.wait(5)
        assert ctx.shell_approval_workflow.status("approval")["continuation"] == "started"
        monkeypatch.setattr(ctx.shell_approval_workflow, "_read_result", Mock(side_effect=ValueError("digest mismatch")))
        state = ctx.shell_approval_workflow.status("approval")
        assert state["continuation"] == "unavailable", "a failed continuation hid its missing result receipt"
        assert ctx.shell_approval_workflow.retry("approval", submit)[0] == 409
        assert submit.call_count == 0
        assert execute.call_count == int(approved)
    finally:
        ctx.close()


def test_background_shell_owns_visible_busy_state_until_it_settles(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx, continue_after=False)
    entered, release = threading.Event(), threading.Event()

    def execute(*_):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "status": "exit", "output": "done", "exit_code": 0}

    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=Mock())
        assert entered.wait(5)
        snapshot = ctx.run_state_payload()
        assert snapshot["busy"] is True, "Stop disappears while background command is still running"
        assert snapshot["session_id"] == "session"
        assert snapshot["run_id"] == "source"
        release.set()
        assert ctx.shell_approval_workflow.wait(5)
        assert ctx.run_state_payload()["busy"] is False
    finally:
        release.set()
        ctx.close()


def test_stop_before_continuation_admission_cannot_be_undone_by_new_work(tmp_path, monkeypatch):
    from codey.app import task_submit

    ctx = AppContext(tmp_path)
    seed(ctx)
    monkeypatch.setattr(
        shell_service,
        "execute_shell_ticket",
        Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0}),
    )
    monkeypatch.setattr(ctx.providers.model_preferences, "allows", lambda *_: True)
    queued = Mock(return_value=True)
    monkeypatch.setattr(task_submit, "submit_browser_task", queued)

    def submit(*args, **kwargs):
        ctx.request_stop()
        ctx.run_registry.stop_flag.clear()
        return task_submit.submit_task(*args, get_state=lambda: ctx, **kwargs)

    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=submit)
        assert ctx.shell_approval_workflow.wait(5)
        queued.assert_called_once()
        assert ctx.shell_approval_workflow.status("approval")["continuation"] == "started"
    finally:
        active = ctx.current_run()
        if active is not None:
            ctx.release_run(active.run_id)
        ctx.close()


def test_shutdown_between_result_and_resume_preserves_delivery_for_restart(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    seed(ctx)
    execute = Mock(return_value={"ok": True, "status": "exit", "output": "done", "exit_code": 0})
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    entered, release = threading.Event(), threading.Event()
    original = ctx.record_shell_result

    def publish(event):
        original(event)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(ctx, "record_shell_result", publish)
    submit = Mock(return_value="resumed")
    closer = threading.Thread(target=ctx.close)
    try:
        api.shell_approval_response(ctx, {"id": "approval", "approved": True}, submit_task_after_slot_release=submit)
        assert entered.wait(5)
        closer.start()
        assert ctx.run_registry.stop_flag.wait(5)
        release.set()
        closer.join(5)
        assert not closer.is_alive() and ctx.closed
        submit.assert_not_called()
        assert ctx.shell_approval_records.latest("approval").kind == "execution_settled"
        restarted = AppContext(tmp_path)
        try:
            restarted.shell_approval_workflow.start(submit)
            assert restarted.shell_approval_workflow.wait(5)
            assert restarted.shell_approval_workflow.status("approval")["continuation"] == "started"
            assert execute.call_count == 1
        finally:
            restarted.close()
    finally:
        release.set()
        if closer.ident:
            closer.join(5)
        ctx.close()
