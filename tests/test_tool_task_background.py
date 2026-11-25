from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.task_submit import dispatch_durable_task
from codey.durable.model import (
    TASK_TERMINAL_STATUSES,
    CommitResult,
    DurableMutation,
    SubmissionStatus,
    TaskStatus,
    valid_frozen_task_state,
)
from codey.durable.tool_task import execution_effect_id, tool_task_id
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot, normalize_turn
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.operations.tool_task_delivery import active_tool_task_controls
from codey.operations.tool_tasks import control_tool_task, run_tool_task, wait_tool_task
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.storage.execution_output_store import ExecutionOutputObserver
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import decode_json_object, parse_json_object, require_json_string


@dataclass(frozen=True)
class Runtime:
    app: AppContext
    project: Path
    session: TaskSession
    snapshot: TurnSnapshot
    sink: KernelEffectSink

    def execute(self, call: ToolCall, *, turn: int = 0) -> ToolResult:
        if turn:
            from codey.runtime.write.checkpoint import operation_from_checkpoint

            state = operation_from_checkpoint(self.app.durable.task("r"), "s")
            assert state is not None
            self.app.runtime_mutations.advance_execution("s", "r", expected=state, leaf="writer_running")
        return execute_turn(self.session, [call], run_id="r", turn=turn, session_id="s", project_path=self.project,
            snapshot=self.snapshot, durable=self.app.durable, intent_sink=self.sink,
            managed_outputs=self.app.managed_outputs, workspace_revision_store=self.app.workspace_revisions)[0]

    def accept(self, command: str = "python -m pytest -q") -> str:
        result = self.execute(ToolCall("run", {"command": command, "run_in_background": True}, "native-run"))
        assert result.ok, result.model_text
        assert result.kind == "accepted", result.model_text
        value = parse_json_object(result.model_text)
        return require_json_string(value["task_ref"], "task_ref")


@pytest.fixture()
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    opened = open_runtime(tmp_path)
    try:
        yield opened
    finally:
        assert opened.app.close()


def open_runtime(tmp_path: Path) -> Runtime:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    app = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    durable = app.durable
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="run tests", provider_snapshot={})
    durable.claim_task("r")
    durable.start_task("r", state={})
    app.runtime_mutations.accept_operation(session_id="s", run_id="r", project=project, provider_id="test",
        turn_budget=8, max_repair_rounds=1, task_kind="project")
    app.runtime_mutations.mark_writer_running("s", "r", provider_id="test")
    session = TaskSession(TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
                          project=str(project))
    snapshot = build_turn_snapshot(session, native=True)
    sink = KernelEffectSink(app.runtime_mutations, session_id="s", run_id="r", provider_id="test",
                            durable=durable, managed_outputs=app.managed_outputs)
    return Runtime(app, project, session, snapshot, sink)


def test_acceptance_is_atomic_and_replays_one_child_without_verification(runtime: Runtime) -> None:
    ref = runtime.accept()
    durable = runtime.app.durable
    origin = turn_effect_id("r", 0, 0)
    child = durable.task(ref)
    assert ref == tool_task_id(origin)
    assert child.owner_task_id == "r" and child.submission_id == durable.task("r").submission_id
    assert durable.effect(origin).settlement is not None
    assert durable.effects(ref) == ()
    assert not runtime.session.verifications
    assert runtime.session.executed[origin]["ok"] is False
    assert runtime.accept() == ref
    assert len(durable.owned_tasks("r")) == 1
    row = RuntimeEffectStore(durable=durable).load_effects("s", "r")[0]
    restored = restore_result_receipt(row, store=runtime.app.managed_outputs, session_id="s", run_id="r")
    assert restored.kind == "accepted"


def test_stdin_only_task_exposes_input_without_terminal_controls(runtime: Runtime) -> None:
    durable = runtime.app.durable
    assert active_tool_task_controls(durable, "r") == frozenset()
    result = runtime.execute(ToolCall("run", {
        "command": "python -m pytest -q", "run_in_background": True, "stdin": True,
    }, "input-run"))
    assert result.ok and result.kind == "accepted", result.model_text
    assert active_tool_task_controls(durable, "r") == {"task_output", "task_cancel", "task_input"}


def test_admission_prompt_and_receipt_reader_point_to_task_output(runtime: Runtime) -> None:
    from codey.operations.kernel_prompt import _result_context, working_context
    from codey.operations.tool_result_reader import ReceiptReader, read_tool_result

    ref = runtime.accept()
    identity = turn_effect_id("r", 0, 0)
    accepted = runtime.session._memory_results[identity]
    prompt = _result_context(accepted, runtime.session)
    assert 'task_output({"task_ref": "' + ref in prompt
    assert "Receipt display handle" not in prompt
    observations = parse_json_object(working_context(runtime.session).splitlines()[1])
    executions = observations["executions"]
    assert isinstance(executions, list)
    row = decode_json_object(executions[0])
    assert row["status"] == "accepted" and row["result_available"] is False
    assert row["task_ref"] == ref
    page = read_tool_result(ReceiptReader(runtime.session, runtime.app.managed_outputs, "s", "r"),
                           ToolCall("read_tool_result", {"result_ref": identity}))
    payload = parse_json_object(page.model_text)
    next_read = decode_json_object(payload["next_read"])
    assert next_read["tool"] == "task_output"
    assert decode_json_object(next_read["args"])["task_ref"] == ref
    assert payload["kind"] == "accepted" and payload["exit_code"] is None
    reader = ReceiptReader(runtime.session, runtime.app.managed_outputs, "s", "r")
    lookup = ToolCall("read_tool_result", {"result_ref": ref})
    alias = read_tool_result(reader, lookup, durable=runtime.app.durable)
    assert alias.call == lookup and alias.ok
    assert parse_json_object(alias.model_text)["task_ref"] == ref
    intruder = ReceiptReader(runtime.session, runtime.app.managed_outputs, "s", ref)
    assert not read_tool_result(intruder, lookup, durable=runtime.app.durable).ok


def test_admission_events_report_accepted_without_a_command_exit(runtime: Runtime) -> None:
    from codey.app.event_payloads import machine_event_payload
    from codey.operations.kernel_events import _emit_tool_results
    from codey.runtime.observe.events import RunEvent, run_event_ui_payload

    runtime.accept()
    accepted = runtime.session._memory_results[turn_effect_id("r", 0, 0)]
    events: list[RunEvent] = []
    _emit_tool_results(events.append, runtime.session, [accepted], run_id="r", turn=0)
    ui = run_event_ui_payload("r", "s", events[0])
    assert ui is not None
    assert ui["status"] == "accepted" and ui["ok"] is True and ui["error"] is False
    assert "exit_code" not in ui
    machine = machine_event_payload(ui)
    assert machine is not None
    assert machine["status"] == "accepted" and machine["ok"] is True
    assert "exit_code" not in machine
    assert runtime.session.executed[turn_effect_id("r", 0, 0)]["ok"] is False


def test_uniform_indent_is_recovered_but_internal_indent_is_rejected(runtime: Runtime) -> None:
    source = "class Checkout:\n    def check(self):\n        if True:\n            raise ValueError('stock')\n"
    path = runtime.project / "checkout.py"
    path.write_text(source, encoding="utf-8")
    assert runtime.execute(ToolCall("read_file", {"path": "checkout.py"})).ok
    wrong = "    if True:\n        raise ValueError('stock')\n"
    inconsistent = "    if True:\n    raise ValueError('stock')\n"
    failed = runtime.execute(ToolCall("edit", {"path": "checkout.py", "replacements": [
        {"old_string": inconsistent, "new_string": inconsistent.replace("stock", "inventory")}] }), turn=1)
    assert not failed.ok
    assert path.read_text(encoding="utf-8") == source
    assert "SEARCH text not found" in failed.model_text
    recovered = runtime.execute(ToolCall("edit", {"path": "checkout.py", "replacements": [
        {"old_string": wrong, "new_string": wrong.replace("stock", "inventory")}] }), turn=2)
    assert recovered.ok, recovered.model_text
    assert recovered.audit["indentation_adjusted"] == 1
    assert path.read_text(encoding="utf-8") == source.replace("stock", "inventory")
    exact = "        if True:\n            raise ValueError('inventory')\n"
    edited = runtime.execute(ToolCall("edit", {"path": "checkout.py", "replacements": [
        {"old_string": exact, "new_string": exact.replace("inventory", "available")}] }), turn=3)
    assert edited.ok, edited.model_text
    assert path.read_text(encoding="utf-8") == source.replace("stock", "available")


def test_background_failure_has_tail_details_in_first_observation(runtime: Runtime) -> None:
    (runtime.project / "test_sample.py").write_text(
        "def test_fail():\n    print('setup-noise ' * 2000)\n    assert 1 == 2\n", encoding="utf-8")
    ref = runtime.accept()
    child = runtime.app.durable.claim_task_checkpoint(ref)
    run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
    observed = runtime.execute(ToolCall("task_output", {"task_ref": ref}), turn=1)
    value = parse_json_object(observed.model_text)
    result = decode_json_object(value["result"])
    text = require_json_string(result["text"], "text")
    assert "FAILED test_sample.py::test_fail" in text and "1 failed" in text
    assert "setup-noise" not in text
    assert result["exit_code"] == 1 and result["ok"] is False
    assert len(runtime.app.durable.effects(ref)) == 1
    assert runtime.app.durable.task("r").status == TaskStatus.RUNNING


def test_guarded_full_read_update_reuses_existing_version_contract(runtime: Runtime) -> None:
    source = "class Checkout:\n    def check(self):\n        return 1\n"
    path = runtime.project / "checkout.py"
    path.write_text(source, encoding="utf-8")
    read = runtime.execute(ToolCall("read_file", {"path": "checkout.py"}))
    assert read.ok
    updated = runtime.execute(ToolCall("edit", {"path": "checkout.py", "content": source.replace("return 1", "return 2"),
        "base_result_ref": turn_effect_id("r", 0, 0)}), turn=1)
    assert updated.ok, updated.model_text
    assert path.read_text(encoding="utf-8") == source.replace("return 1", "return 2")


def test_failed_anchor_reoffers_definition_after_current_file_read(runtime: Runtime) -> None:
    from codey.operations.edit_base import bound_symbol_retry, edit_anchor_retry
    from codey.providers.base import AssistantTurn, ProviderToolCall

    path = runtime.project / "worker.py"
    original = "def current():\n    return 1\n\ndef untouched():\n    return 2\n"
    path.write_text(original, encoding="utf-8")
    assert runtime.execute(ToolCall("read_file", {"path": path.name})).ok
    stale = ToolCall("edit", {"path": path.name, "replacements": [
        {"old_string": "def current():\n    return 0", "new_string": "def current():\n    return 3"}]})
    failed = runtime.execute(stale, turn=1)
    assert not failed.ok and failed.audit["error_code"] == "edit_search_not_found"
    snapshot = build_turn_snapshot(runtime.session, native=True)
    bound, hint = edit_anchor_retry(runtime.session, snapshot, (failed,))
    assert bound_symbol_retry(bound) == ("worker.py", "current")
    assert "return 1" in hint and "untouched" not in hint
    path.write_text(original.replace("return 1", "return 8"), encoding="utf-8")
    _, stale_hint = edit_anchor_retry(runtime.session, snapshot, (failed,))
    assert '"tool":"read_file"' in stale_hint
    read = runtime.execute(ToolCall("read_file", {"path": path.name}), turn=2)
    assert read.ok
    bound, hint = edit_anchor_retry(runtime.session, build_turn_snapshot(runtime.session, native=True), (read,))
    assert "return 8" in hint and "untouched" not in hint
    content = "def current():\n    return 3"
    assert normalize_turn(AssistantTurn(text=content), snapshot=bound).protocol_error
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("edit-original", "edit", {"content": content}),)),
                          snapshot=bound)
    assert not plan.protocol_error and plan.calls[0].args["symbol"] == "current"
    assert plan.calls[0].call_id == "edit-original"
    updated = runtime.execute(plan.calls[0], turn=3)
    assert updated.ok and path.read_text(encoding="utf-8") == original.replace("return 1", "return 3")


def test_cancel_child_does_not_cancel_user_submission(runtime: Runtime) -> None:
    ref = runtime.accept()
    result = runtime.execute(ToolCall("task_cancel", {"task_ref": ref}), turn=1)
    assert result.ok, result.model_text
    assert runtime.app.durable.task(ref).status == TaskStatus.CANCELLED
    submission = runtime.app.durable.store.submission(runtime.app.durable.task("r").submission_id)
    assert submission.status == SubmissionStatus.RUNNING
    assert runtime.app.durable.task("r").status == TaskStatus.RUNNING


def test_real_background_command_uses_original_runtime_and_one_result(runtime: Runtime) -> None:
    (runtime.project / "test_sample.py").write_text("def test_ok():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    ref = runtime.accept()
    child = runtime.app.durable.claim_task_checkpoint(ref)
    run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
    completed = runtime.app.durable.task(ref)
    assert completed.status == TaskStatus.COMPLETED
    effects = runtime.app.durable.effects(ref)
    assert len(effects) == 1 and effects[0].effect_id == execution_effect_id(ref)
    assert effects[0].status == "ok", effects[0].settlement
    result = runtime.execute(ToolCall("task_output", {"task_ref": ref}), turn=1)
    assert result.ok, result.model_text
    value = parse_json_object(result.model_text)
    result_value = decode_json_object(value["result"])
    assert result_value["ok"] is True
    assert result_value["exit_code"] == 0
    assert runtime.app.durable.store.submission(completed.submission_id).status == SubmissionStatus.RUNNING


def test_unknown_reference_and_other_owner_cannot_control_work(runtime: Runtime) -> None:
    ref = runtime.accept()
    runtime.app.durable.admit_input(conversation_id="other", request_id="other", task_id="other", text="x", provider_snapshot={})
    denied = control_tool_task(ToolCall("task_cancel", {"task_ref": ref}), durable=runtime.app.durable,
        owner_task_id="other", store=runtime.app.managed_outputs)
    assert not denied.ok
    assert not runtime.app.durable.task(ref).state.get("cancel_requested")


def test_wait_timeout_and_independent_utf8_output_cursors(runtime: Runtime) -> None:
    ref = runtime.accept()
    current = wait_tool_task(runtime.app.durable, ref, owner_task_id="r", wait_ms=10)
    assert current.status == TaskStatus.PENDING
    assert not current.state.get("cancel_requested")
    store = runtime.app.execution_outputs
    store.create(execution_effect_id(ref), session_id="s", run_id=ref, max_bytes=48)
    observer = ExecutionOutputObserver(store, execution_effect_id(ref))
    raw = "你好世界".encode()
    observer.offer("stdout", raw[:2])
    observer.offer("stdout", raw[2:])
    observer.close()
    first = store.read_since(execution_effect_id(ref), "stdout", limit=2)
    again = store.read_since(execution_effect_id(ref), "stdout", limit=2)
    assert first == again
    assert first["next_cursor"] == 6
    assert store.read_since(execution_effect_id(ref), "stdout", 6)["next_cursor"] == 12


def test_snapshot_and_argument_contracts_agree(runtime: Runtime) -> None:
    restored = restore_snapshot(snapshot_description(runtime.snapshot))
    spec = next(spec for spec in restored.frozen_specs if spec.name == "run")
    assert spec.supports_background
    from codey.toolchain.tool_prompt import json_contract_text, model_tool_description

    example = spec.description.split("Background call example: ", 1)[1]
    background_call = parse_json_object(example)
    assert background_call["tool"] == "run"
    assert decode_json_object(background_call["args"])["run_in_background"] is True
    assert example in json_contract_text((spec,))
    assert example in model_tool_description(spec)
    assert not validate_args_with_spec(spec, decode_json_object(background_call["args"]))
    args = {"command": "python -m pytest -q", "run_in_background": True}
    assert not validate_args_with_spec(spec, args)
    plan = normalize_turn(decode_text_turn('{"tool":"run","args":{"command":"python -m pytest -q","run_in_background":true}}'),
                          snapshot=restored)
    assert not plan.protocol_error, plan.protocol_error
    assert plan.calls[0].args["run_in_background"] is True
    denied = normalize_turn(decode_text_turn('{"tool":"read_file","args":{"path":"a.py","run_in_background":true}}'), snapshot=restored)
    assert denied.protocol_error
    assert not valid_frozen_task_state({}, {"execution_kind": "tool"})


def test_scheduler_child_bypasses_parent_model_slot(runtime: Runtime) -> None:
    (runtime.project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    ref = runtime.accept()
    runtime.app.reserve_run(session_id="s", project=str(runtime.project), task="parent", provider_id="test", run_id="r")
    child = runtime.app.durable.claim_task_checkpoint(ref)
    assert dispatch_durable_task(child, state=runtime.app)
    assert dispatch_durable_task(child, state=runtime.app)
    terminal = wait_tool_task(runtime.app.durable, ref, owner_task_id="r", wait_ms=15000)
    assert terminal.status in TASK_TERMINAL_STATUSES
    assert terminal.status == TaskStatus.COMPLETED, dict(terminal.state)
    assert len(runtime.app.durable.effects(ref)) == 1


def test_stop_running_process_cleans_up_and_preserves_parent(runtime: Runtime) -> None:
    (runtime.project / "test_sample.py").write_text(
        "import time\ndef test_slow():\n    print('live-output', flush=True)\n    time.sleep(20)\n", encoding="utf-8")
    ref = runtime.accept("python -m pytest -q -s")
    child = runtime.app.durable.claim_task_checkpoint(ref)
    assert dispatch_durable_task(child, state=runtime.app)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = control_tool_task(ToolCall("task_output", {"task_ref": ref}), durable=runtime.app.durable,
            owner_task_id="r", store=runtime.app.managed_outputs)
        if "live-output" in result.model_text:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"background process produced no live output: {result.model_text}")
    result = runtime.execute(ToolCall("task_cancel", {"task_ref": ref, "wait_ms": 10000}), turn=1)
    assert result.ok, result.model_text
    assert runtime.app.durable.task(ref).status == TaskStatus.CANCELLED
    assert runtime.app.durable.store.submission(runtime.app.durable.task("r").submission_id).status == SubmissionStatus.RUNNING


def test_executor_cannot_forge_acceptance(runtime: Runtime) -> None:
    produced = ToolResult(ToolCall("run", {"command": "python -m pytest -q"}), "fake", ok=True, kind="accepted")
    from codey.operations.kernel_result import _consistent_tool_result

    assert not _consistent_tool_result(produced.call, produced).ok


def test_coding_context_distinguishes_failed_check_from_unrun_check(runtime: Runtime) -> None:
    from codey.operations.project_prompt_context import prepare_coding_context

    (runtime.project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result = runtime.execute(ToolCall("edit", {"path": "test_sample.py", "content": "def test_sample():\n    assert False\n"}))
    assert result.ok, result.model_text
    before = prepare_coding_context(runtime.session)
    assert before is not None and not before.verification_failed
    # This fixture deliberately has no scheduler; this test exercises inline
    # verification projection, not automatic command scheduling.
    failed = runtime.execute(ToolCall("run", {"command": "python -m pytest -q", "run_in_background": False}), turn=1)
    assert not failed.ok, failed.model_text
    after = prepare_coding_context(runtime.session)
    assert after is not None and after.verification_failed
    assert not after.verification_fresh
    assert runtime.execute(ToolCall("read_file", {"path": "test_sample.py"}), turn=2).ok
    assert runtime.execute(ToolCall("edit", {"path": "test_sample.py", "replacements": [
        {"old_string": "    assert False", "new_string": "    assert True"}]}), turn=3).ok
    changed = prepare_coding_context(runtime.session)
    assert changed is not None and not changed.verification_failed and not changed.verification_fresh
    assert runtime.execute(ToolCall("run", {"command": "python -m pytest -q", "run_in_background": False}), turn=4).ok
    passed = prepare_coding_context(runtime.session)
    assert passed is not None and passed.verification_fresh and not passed.verification_failed


def test_tool_kind_and_input_cannot_change_after_admission(runtime: Runtime) -> None:
    ref = runtime.accept()
    child = runtime.app.durable.task(ref)
    with pytest.raises(ValueError):
        replace(child, state={**child.state, "execution_kind": "other"})
    with pytest.raises(ValueError, match="cannot answer"):
        runtime.app.durable.advance_task(task_id=ref, phase="running", state={}, answer="wrong")


def test_failed_admission_rolls_back_receipt_child_and_execution_position(runtime: Runtime) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore

    assert runtime.app.state_home is not None
    with closing(sqlite3.connect(runtime.app.state_home / "durable.sqlite3")) as database:
        database.execute("""CREATE TRIGGER reject_tool_task BEFORE INSERT ON tasks
            WHEN json_extract(NEW.state, '$.execution_kind') = 'tool'
            BEGIN SELECT RAISE(ABORT, 'injected admission failure'); END""")
        database.commit()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected admission failure"):
            runtime.accept()
        origin = turn_effect_id("r", 0, 0)
        reopened = DurableSession(SQLiteDurableStore(runtime.app.state_home / "durable.sqlite3"))
        try:
            assert reopened.effect(origin).settlement is None
            assert reopened.owned_tasks("r") == ()
        finally:
            reopened.close()
        assert not runtime.session.verifications
    finally:
        with closing(sqlite3.connect(runtime.app.state_home / "durable.sqlite3")) as database:
            database.execute("DROP TRIGGER reject_tool_task")
            database.commit()


def crash_probe(root: Path, boundary: str) -> None:
    opened = open_runtime(root)

    def crash(_result: CommitResult, mutation: DurableMutation) -> None:
        if boundary in {"admission", "auto_admission"} and mutation.task_updates:
            os._exit(73)
        if mutation.task is None or mutation.task.execution_kind != "tool":
            return
        if boundary in {"intent", "cancel"} and mutation.effects:
            if boundary == "cancel":
                opened.app.durable.request_cancel(mutation.task.task_id)
            os._exit(73)
        if boundary == "receipt" and mutation.effect is not None and mutation.effect.settlement is not None:
            os._exit(73)

    opened.app.durable.subscribe_commits(crash)
    if boundary == "auto_admission":
        opened.execute(ToolCall("run", {"command": "python -m pytest -q"}))
        raise AssertionError("automatic admission crash boundary was not reached")
    ref = opened.accept()
    child = opened.app.durable.claim_task_checkpoint(ref)
    run_tool_task(opened.app.durable, child, stores=opened.app.task_submission_stores, signal=threading.Event())
    raise AssertionError("crash boundary was not reached")


@pytest.mark.parametrize("boundary", ["admission", "auto_admission", "intent", "receipt", "cancel"])
def test_process_restart_never_repeats_an_uncertain_or_settled_execution(tmp_path: Path, boundary: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_once.py").write_text(
        "from pathlib import Path\ndef test_once():\n"
        "    counter = Path('counter.txt')\n"
        "    count = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(count + 1))\n", encoding="utf-8")
    script = ("from pathlib import Path; import sys; "
              "from tests.test_tool_task_background import crash_probe; crash_probe(Path(sys.argv[1]), sys.argv[2])")
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path), boundary], capture_output=True,
                             text=True, timeout=30)
    assert crashed.returncode == 73, (crashed.stdout, crashed.stderr)
    app = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    try:
        app.durable.recover_scheduler_state()
        ref = tool_task_id(turn_effect_id("r", 0, 0))
        assert app.durable.task(ref).status == TaskStatus.PENDING
        child = app.durable.claim_task_checkpoint(ref)
        run_tool_task(app.durable, child, stores=app.task_submission_stores, signal=threading.Event())
        if boundary in {"intent", "cancel"}:
            assert not (project / "counter.txt").exists()
            assert app.durable.task(ref).status == TaskStatus.FAILED
            assert app.durable.task(ref).state["cleanup_unknown"] is True
            assert app.durable.effect(execution_effect_id(ref)).status == "interrupted"
        else:
            assert (project / "counter.txt").read_text() == "1"
            if boundary == "auto_admission":
                from codey.operations.tool_tasks import recover_command_admission

                origin = turn_effect_id("r", 0, 0)
                assert app.durable.effect(origin).settlement is None
                recovered = recover_command_admission(app.durable, origin_effect_id=origin,
                    owner_task_id="r", store=app.managed_outputs)
                assert recovered is not None and recovered.ok, recovered
                assert len(app.durable.owned_tasks("r")) == 1
            assert app.durable.task(ref).status == TaskStatus.COMPLETED
        assert len(app.durable.effects(ref)) == 1
    finally:
        assert app.close()


def test_parent_failure_drains_live_tool_and_preserves_failure_reason(runtime: Runtime) -> None:
    ref = runtime.accept()
    child = runtime.app.durable.claim_task_checkpoint(ref)
    runtime.app.durable.fail_task("r", "original provider failure", state={})
    assert runtime.app.durable.task("r").status == TaskStatus.COMPLETING
    assert runtime.app.durable.task(ref).state["cancel_requested"] is True
    run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
    runtime.app.durable.reconcile_finalization()
    assert runtime.app.durable.task(ref).status == TaskStatus.CANCELLED
    assert runtime.app.durable.task("r").status == TaskStatus.FAILED
    assert runtime.app.durable.store.submission(child.submission_id).reason == "original provider failure"


def test_invalid_captured_worker_fails_without_touching_submission(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations import provider_receipts

    ref = runtime.accept()
    child = runtime.app.durable.claim_task_checkpoint(ref)

    def unavailable(_payload: object) -> TurnSnapshot:
        raise ValueError("captured implementation unavailable")

    monkeypatch.setattr(provider_receipts, "restore_snapshot", unavailable)
    run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
    assert runtime.app.durable.task(ref).status == TaskStatus.FAILED
    assert runtime.app.durable.task(ref).state["tool_worker_active"] is False
    assert runtime.app.durable.effects(ref) == ()
    assert runtime.app.durable.store.submission(child.submission_id).status == SubmissionStatus.RUNNING


def test_incremental_output_reports_gap_after_multiple_trims(runtime: Runtime) -> None:
    from codey.storage.execution_output_store import ExecutionOutputStore

    store = runtime.app.execution_outputs
    store.create("trimmed", session_id="s", run_id="r", max_bytes=16)
    store.append_text("trimmed", "stdout", "0123456789abcdef")
    store.append_text("trimmed", "stdout", "ghijklmnop")
    store.append_text("trimmed", "stdout", "qrstuvwxyz")
    output = store.read_since("trimmed", "stdout")
    assert output["chunks"] == [{"start": 0, "text": "0123"}, {"start": 24, "text": "opqrstuvwxyz"}]
    assert output["gaps"] == [{"start": 4, "end": 24}]
    assert output["next_cursor"] == 36
    assert ExecutionOutputStore.stream_since(store.snapshot("trimmed"), "stdout") == output


def test_codemode_values_use_acceptance_union_and_completed_branch(runtime: Runtime) -> None:
    from codey.codemode.catalog import build_catalog_from_specs
    from codey.codemode.host import successful_tool_value

    spec = next(spec for spec in runtime.snapshot.frozen_specs if spec.name == "run")
    entry = build_catalog_from_specs([spec]).entries[0]
    accepted = runtime.execute(ToolCall("run", {"command": "python -m pytest -q", "run_in_background": True}))
    value = decode_json_object(successful_tool_value(accepted, entry))
    assert value["kind"] == "accepted"
    completed = ToolResult(ToolCall("run", {"command": "python -m pytest -q"}), "done", ok=True)
    assert successful_tool_value(completed, entry) == "done"


def test_formal_child_result_restores_verification_once(runtime: Runtime) -> None:
    from codey.operations.kernel_context import KernelContext
    from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
    from codey.providers.requests import ProviderRequest
    from codey.providers.transport import ProviderTransport

    class NoCalls(ProviderTransport):
        provider: object = None

        def execute_request(self, request: ProviderRequest) -> object:
            raise AssertionError("restoring facts must not call a provider")

    (runtime.project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    ref = runtime.accept()
    child = runtime.app.durable.claim_task_checkpoint(ref)
    run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
    restored = TaskSession(runtime.session.policy, project=str(runtime.project))
    context = KernelContext(restored, KernelRunRequest(KernelTransportDeps(NoCalls(), run_id="r"),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
                            workspace_revision_store=runtime.app.workspace_revisions)))
    context.restore_facts()
    context.restore_facts()
    assert len(restored.verifications) == 1
    assert restored.executed[execution_effect_id(ref)]["ok"] is True
    assert restored.executed[turn_effect_id("r", 0, 0)]["ok"] is False


@pytest.mark.parametrize("native,nested,premature,editing", [
    (False, False, False, False), (True, False, False, False),
    (True, True, False, False), (False, False, True, False),
    (False, False, False, True), (True, False, False, True)])
def test_real_kernel_and_scheduler_accept_observe_and_complete(
        runtime: Runtime, native: bool, nested: bool, premature: bool, editing: bool) -> None:
    from collections.abc import Sequence

    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.operations.provider_session import adapt_provider
    from codey.operations.task_loop import run_task_kernel
    from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult

    class Scripted:
        name = "test"
        native_tools = native

        def __init__(self) -> None:
            self.turn = 0
            self.delivered_ids: list[str] = []

        def next_call(self) -> ToolCall:
            self.turn += 1
            if editing and self.turn == 1:
                return ToolCall("read_file", {"path": "value.py"})
            if editing and self.turn == 2:
                return ToolCall("edit", {"path": "value.py", "replacements": [
                    {"old_string": "VALUE = 1", "new_string": "VALUE = 2"}]})
            turn = self.turn - (2 if editing else 0)
            if turn == 1:
                if nested:
                    return ToolCall("codemode", {"code": "return await tools.run({command:'python -m pytest -q', run_in_background:true});"})
                return ToolCall("run", {"command": "python -m pytest -q", "run_in_background": True})
            if turn == 2 and premature:
                return ToolCall("done", {"summary": "Tests completed successfully."})
            if turn == (3 if premature else 2):
                children = runtime.app.durable.owned_tasks("r")
                assert len(children) == 1
                return ToolCall("task_output", {"task_ref": children[0].task_id, "wait_ms": 15000})
            assert turn == (4 if premature else 3), "unexpected extra provider round"
            return ToolCall("done", {"summary": "Tests completed successfully."})

        def send(self, prompt: str) -> str:
            call = self.next_call()
            value: dict[str, object] = {"tool": call.name, "args": call.args}
            return json.dumps(value)

        def send_turn(self, prompt: str, tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
            call = self.next_call()
            return AssistantTurn(tool_calls=(ProviderToolCall(f"call-{self.turn}", call.name, call.args),))

        def send_tool_results(self, results: Sequence[ProviderToolResult], tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
            self.delivered_ids.extend(result.call_id for result in results)
            return self.send_turn("continue", tools)

        def acknowledge_tool_results(self, results: Sequence[ProviderToolResult], tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
            self.delivered_ids.extend(result.call_id for result in results)
            return AssistantTurn(text="closed")

    if editing:
        from codey.completion.verification_policy import VerificationCandidate

        (runtime.project / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        (runtime.project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (runtime.project / "test_sample.py").write_text("from value import VALUE\ndef test_ok():\n    assert VALUE == 2\n", encoding="utf-8")
        runtime.session.project_changes_required = True
        runtime.session.selected_verification = VerificationCandidate("python -m pytest", ".")
    else:
        (runtime.project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    provider = Scripted()
    runtime.app.durable_scheduler.start()
    result = run_task_kernel(runtime.session, request=KernelRunRequest(
        KernelTransportDeps(adapt_provider(provider), run_id="r", provider_id="test", user_task="Run the tests."),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
                            workspace_revision_store=runtime.app.workspace_revisions),
        KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=runtime.app.durable, session_id="s", task_id="r", sink=runtime.sink))))
    assert result.completed, result
    assert provider.turn == (4 if premature else 3) + (2 if editing else 0)
    children = runtime.app.durable.owned_tasks("r")
    assert len(children) == 1 and children[0].status == TaskStatus.COMPLETED
    assert len(runtime.session.verifications) == 1
    if native:
        assert provider.delivered_ids == [f"call-{index}" for index in range(1, provider.turn + 1)]


def test_cold_start_contract_rejects_old_snapshot_or_missing_capability(runtime: Runtime) -> None:
    payload = snapshot_description(runtime.snapshot)
    with pytest.raises(ValueError, match="unsupported captured turn snapshot"):
        restore_snapshot({**payload, "schema_version": 9})
    specs = payload["specs"]
    assert isinstance(specs, list)
    row = decode_json_object(specs[0])
    row.pop("supports_background")
    specs[0] = row
    with pytest.raises(ValueError, match="invalid captured tool specification"):
        restore_snapshot(payload)


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
def test_headless_entry_starts_child_without_dispatching_another_model(tmp_path: Path) -> None:
    from collections.abc import Callable

    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget

    project = tmp_path / "project"
    project.mkdir()
    (project / "test_ready.py").write_text("def test_ready():\n    assert True\n", encoding="utf-8")

    class Scripted:
        name = "Local"
        location = "scripted"
        context_budget = ContextBudget(32768, 8192, 0, 12000)

        def bind_usage(self, connection_id: str, sink: Callable[[ApiExchangeUsage], None]) -> None:
            pass

        def __init__(self) -> None:
            self.turn = 0

        def new_chat(self, timeout: float | None = None) -> None:
            pass

        def send(self, text: str, timeout: float | None = None) -> str:
            self.turn += 1
            if self.turn == 1:
                start: dict[str, object] = {"tool": "run", "args": {"command": "python -m pytest -q", "run_in_background": True}}
                return json.dumps(start)
            if self.turn == 2:
                accepted = re.search(r"tool_task:[0-9a-f]+", text)
                assert accepted is not None, text
                query: dict[str, object] = {"tool": "task_output", "args": {"task_ref": accepted.group(), "wait_ms": 5000}}
                return json.dumps(query)
            assert self.turn == 3, text
            assert '"status": "completed"' in text, text
            done: dict[str, object] = {"tool": "done", "args": {"summary": "Tests passed."}}
            return json.dumps(done)

        def close(self) -> None:
            pass

    provider = Scripted()
    def connect(provider_id: str, *, port: int) -> Scripted:
        return provider

    rows: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(project, "后台运行测试，查看结果，再告诉我结果。不要修改文件。", provider_id="local",
        run_id="headless-background", state_home=tmp_path / "state", max_turns=4, project_changes_required=False),
        emit_jsonl=rows.append, connect_provider=connect)
    assert result.stop_reason == "done", rows
