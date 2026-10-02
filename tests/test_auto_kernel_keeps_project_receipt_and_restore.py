"""Auto uses the same persistent project tracker and terminal receipt contract."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.app.context import AppContext
from codey.operations.auto_loop import run_auto_mode
from codey.operations.context import RunWork
from codey.operations.task_entry import run_entry_kernel
from codey.policies.task_policy import TaskPolicy
from codey.runs.ledger import RunLedgerStore
from codey.runs.ledger_projection import load_run_projection
from codey.runs.receipt import task_receipt_from_payload
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.changes import collect_changes
from tests.test_auto_direct_answer_continues_to_kernel import _auto_deps, _auto_frame, _SeqProvider


@pytest.mark.parametrize("existing", [False, True])
def test_auto_edit_has_one_truthful_receipt_and_survives_restart_restore(tmp_path, monkeypatch, existing):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    project.mkdir()
    path = project / "result.txt"
    if existing:
        path.write_text("original", encoding="utf-8")
    edit = {"path": "result.txt", "content": "changed"}
    replies = ["ACTION: project\nPLAN: Change result.txt and run the tests."]
    if existing:
        replies.append(json.dumps({"tool": "read_file", "args": {"path": "result.txt"}}))
        edit = {"path": "result.txt", "replacements": [{"old_string": "original", "new_string": "changed"}]}
    replies += [json.dumps({"tool": "edit", "args": edit}),
                json.dumps({"tool": "run", "args": {"command": "python -m unittest", "path": "."}}),
                json.dumps({"tool": "done", "args": {"summary": "finished"}})]
    provider = _SeqProvider(replies)
    frame = _auto_frame("Change result.txt and run the tests.", provider, project_changes_required=True)
    frame.request = replace(frame.request, project=str(project))
    frame.project_text = str(project)
    frame.entry_policy = TaskPolicy(frozenset({"control", "project.read", "project.write", "project.verify"}))
    state = AppContext(tmp_path / "state")
    ledger_store = RunLedgerStore(tmp_path / "state")
    ledger = ledger_store.open(session_id="s-auto", run_id="r-auto", project=project,
                               task=frame.request.task, provider="web", mode="agent")
    runtime = SimpleNamespace(state=state, workspace_revisions=state.workspace_revisions,
                              collect_changes=collect_changes)
    hooks = SimpleNamespace(on_event=lambda _: None, on_shell_request=None,
                            append_ledger=lambda record: record(ledger))

    def continuation(active_frame, work, active_hooks, *, followup):
        return run_entry_kernel(active_frame, work, active_hooks, runtime,
                                task_kind="project", continuation_followup=followup)

    deps = replace(_auto_deps(state, SimpleNamespace()), continue_task=continuation)
    outcome = run_auto_mode(frame, RunWork([], ExecutionEvidence(), ledger=ledger), hooks, deps)
    assert outcome.event["stop_reason"] == "done"
    assert outcome.event["mode"] == "agent"
    assert outcome.event["changed"] is True
    assert outcome.event["receipt"]["work"]["changed_count"] == 1
    assert outcome.event["receipt"]["work"]["restore_available"] is True
    assert outcome.event["receipt"]["verification"]["trust"] == "limited"
    assert outcome.event["changes"]["project"] == str(project)
    assert path.read_text(encoding="utf-8") == "changed"
    projection = load_run_projection(ledger_store, "s-auto", "r-auto")
    assert projection.final_changes.receipt == task_receipt_from_payload(outcome.event["receipt"])
    assert outcome.event["receipt"]["completion_proof"]["proof_id"] in projection.final_changes.receipt.verification.proof_refs
    restarted = AppContext(tmp_path / "state")
    tracker = restarted.change_tracker_for(project, persistent=True)
    assert tracker.has_snapshots
    assert tracker.restore().ok
    if existing:
        assert path.read_text(encoding="utf-8") == "original"
    else:
        assert not path.exists()
