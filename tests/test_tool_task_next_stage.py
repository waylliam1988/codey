from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.project_completion_checks import _evidence_with_session_facts
from codey.operations.task_session import TaskSession, session_checks_passed
from codey.operations.tool_tasks import run_tool_task
from codey.providers.requests import ProviderRequest
from codey.providers.transport import ProviderTransport
from codey.runtime.core.models import ToolCall
from codey.runtime.core.tool_resources import tool_resources_for
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.test_tool_task_background import Runtime, open_runtime


class NoCalls(ProviderTransport):
    provider: object = None

    def execute_request(self, request: ProviderRequest) -> object:
        raise AssertionError("fact recovery must not call a model")


def restore(runtime: Runtime, session: TaskSession | None = None) -> TaskSession:
    restored = session if session is not None else TaskSession(runtime.session.policy, project=str(runtime.project))
    context = KernelContext(restored, KernelRunRequest(KernelTransportDeps(NoCalls(), run_id="r"),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
                            workspace_revision_store=runtime.app.workspace_revisions)))
    context.restore_facts()
    return restored


def test_declared_verification_waits_for_same_diagnostic_cleanup(tmp_path: Path) -> None:
    from codey.operations.kernel.kernel_protocol import normalize_turn
    from codey.operations.tool_tasks import control_tool_task, wait_tool_task
    from codey.operations.turn_snapshot import build_turn_snapshot
    from codey.operations.verification_workflow import completion_workflow, verification_workflow
    from codey.protocols.text_turn import decode_text_turn
    from codey.types.json_value import parse_json_object, require_json_string

    runtime = open_runtime(tmp_path)
    worker: threading.Thread | None = None
    stop = threading.Event()
    try:
        runtime.session.task_text = (
            "1. Start python -m pytest -q -s test_order.py in the background.\n"
            "2. Repeat the same regression in the background until it passes.\n"
            "3. Start python -m pytest -q -s probe_order.py in the background. "
            "Once ready-for-cleanup appears, stop only that diagnostic and confirm.\n"
            "4. Run python -m pytest -q test_health.py in the foreground.\n")
        (runtime.project / "test_order.py").write_text("def test_pass():\n    assert True\n", encoding="utf-8")
        (runtime.project / "test_health.py").write_text("def test_health():\n    assert True\n", encoding="utf-8")
        (runtime.project / "probe_order.py").write_text(
            "import time\ndef test_probe():\n    print('ready-for-cleanup', flush=True)\n    time.sleep(30)\n",
            encoding="utf-8")
        durable = runtime.app.durable
        def next_step() -> tuple[ToolCall, str]:
            restore(runtime, runtime.session)
            current, hint = verification_workflow(runtime.session, build_turn_snapshot(runtime.session, native=True),
                durable, "r", runtime.app.managed_outputs, runtime.app.workspace_revisions, ignored_paths=())
            plan = normalize_turn(decode_text_turn(hint.splitlines()[-1]), snapshot=current)
            assert not plan.protocol_error and len(plan.calls) == 1, (plan.protocol_error, hint)
            return plan.calls[0], hint

        first, _ = next_step()
        assert first.args["command"] == "python -m pytest -q -s test_order.py"
        reference = runtime.accept(require_json_string(first.args["command"], "command"))
        run_tool_task(durable, durable.claim_task_checkpoint(reference), stores=runtime.app.task_submission_stores,
                      signal=stop)
        probe, _ = next_step()
        assert probe.args["command"] == "python -m pytest -q -s probe_order.py"
        accepted = runtime.execute(probe, turn=1)
        ref = require_json_string(parse_json_object(accepted.model_text)["task_ref"], "task_ref")
        child = durable.claim_task_checkpoint(ref)
        worker = threading.Thread(target=lambda: run_tool_task(durable, child,
            stores=runtime.app.task_submission_stores, signal=stop))
        worker.start()
        deadline = time.monotonic() + 15
        while True:
            action, hint = next_step()
            assert action.args["task_ref"] == ref
            if action.name == "task_cancel":
                assert "ready-for-cleanup" in hint
                break
            assert action.name == "task_output" and time.monotonic() < deadline, hint
            stop.wait(0.02)
        cancelled = control_tool_task(action, durable=durable, owner_task_id="r", store=runtime.app.managed_outputs)
        assert cancelled.ok and parse_json_object(cancelled.model_text)["status"] == "cancelled"
        assert not wait_tool_task(durable, ref, owner_task_id="r", wait_ms=10000).state.get("cleanup_unknown")
        health, hint = next_step()
        assert health.args["command"] == "python -m pytest -q test_health.py"
        assert health.args["run_in_background"] is False and ref in hint
        assert runtime.execute(health, turn=2).ok
        plan, gap = completion_workflow(runtime.session, durable, "r", runtime.app.managed_outputs)
        assert plan is not None and not gap
        assert [row.candidate.command for row in plan.obligations] == [first.args["command"], health.args["command"]]
    finally:
        stop.set()
        if worker is not None:
            worker.join(15)
        assert runtime.app.close()


@pytest.mark.parametrize(("first_pass", "second_pass"), [(True, False), (False, True)])
def test_recovery_orders_child_and_parent_checks_by_settlement(
    tmp_path: Path, first_pass: bool, second_pass: bool,
) -> None:
    runtime = open_runtime(tmp_path)
    try:
        flag = tmp_path / "external-flag"
        if not first_pass:
            flag.write_text("fail", encoding="utf-8")
        (runtime.project / "test_order.py").write_text(
            "from pathlib import Path\ndef test_order():\n    assert not Path(" + repr(str(flag)) + ").exists()\n",
            encoding="utf-8")
        ref = runtime.accept()
        child = runtime.app.durable.claim_task_checkpoint(ref)
        run_tool_task(runtime.app.durable, child, stores=runtime.app.task_submission_stores, signal=threading.Event())
        if second_pass:
            flag.unlink()
        else:
            flag.write_text("fail", encoding="utf-8")
        failed = runtime.execute(ToolCall("run", {"command": "python -m pytest -q", "run_in_background": False}), turn=1)
        assert failed.ok is second_pass and failed.audit["exit_code"] == (0 if second_pass else 1)
        # The live view has already seen the newer failure before reading the old child.
        hot = restore(runtime, runtime.session)
        cold = restore(runtime)
        for session in (hot, cold):
            assert [row["passed"] for row in session.verifications] == [first_pass, second_pass]
            assert session_checks_passed(session) is second_pass
            restore(runtime, session)
            assert [row["passed"] for row in session.verifications] == [first_pass, second_pass]
    finally:
        assert runtime.app.close()


def test_pending_input_is_not_delivered_again_after_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import turn_effect_id
    from codey.runtime.core.models import ToolResult

    runtime = open_runtime(tmp_path)
    try:
        call = ToolCall("task_input", {"task_ref": "original-process", "data": "do not repeat"})
        identity = turn_effect_id("r", 0, 0)
        runtime.sink.begin_turn([(identity, call, 0)], turn=0,
                                specs={spec.name: spec for spec in runtime.snapshot.frozen_specs})
        restored_sink = KernelEffectSink(runtime.app.runtime_mutations, session_id="s", run_id="r",
            provider_id="test", durable=runtime.app.durable, managed_outputs=runtime.app.managed_outputs)

        def forbidden(call: ToolCall, *, durable: object, owner_task_id: str,
                      store: object, caller_policy: object = None) -> ToolResult:
            raise AssertionError("unsettled input must not reach process control twice")

        monkeypatch.setattr("codey.operations.tool_tasks.control_tool_task", forbidden)
        result = execute_turn(TaskSession(runtime.session.policy, project=str(runtime.project)), [call],
            run_id="r", turn=0, session_id="s", snapshot=runtime.snapshot, project_path=runtime.project,
            durable=runtime.app.durable, intent_sink=restored_sink, managed_outputs=runtime.app.managed_outputs,
            workspace_revision_store=runtime.app.workspace_revisions)[0]
        assert not result.ok
        assert runtime.app.durable.effect(identity).settlement is not None
    finally:
        assert runtime.app.close()


def test_unknown_latest_check_removes_previously_projected_success() -> None:
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(TaskPolicy(grants=frozenset({"project.verify"})))
    fingerprint = "sha256:" + "a" * 64
    session.record_verification("python -m pytest -q", 0, True, exit_code=0,
                                workspace_revision=1, workspace_fingerprint=fingerprint)
    evidence = ExecutionEvidence(workspace_revision=1, workspace_fingerprint=fingerprint)
    _evidence_with_session_facts(evidence, session)
    assert evidence.has_successful_checks
    session.record_verification("python -m pytest -q", 0, False)
    _, gaps = _evidence_with_session_facts(evidence, session)
    assert gaps
    assert not evidence.has_successful_checks


@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("codemode", [False, True])
def test_durable_process_input_dedup_and_cleanup(tmp_path: Path, tty: bool, codemode: bool) -> None:
    import os

    runtime = open_runtime(tmp_path)
    worker: threading.Thread | None = None
    try:
        (runtime.project / "test_input.py").write_text(
            "import sys\ndef test_input():\n"
            "    a=sys.stdin.readline()\n    b=sys.stdin.readline()\n"
            "    assert (a.strip(),b.strip()) == ('one','two')\n"
            "    if not sys.stdin.isatty():\n        assert sys.stdin.read() == ''\n", encoding="utf-8")
        accepted = runtime.execute(ToolCall("run", {"command": "python -m pytest -q -s", "stdin": True,
            "tty": tty, "run_in_background": True}))
        assert accepted.ok and accepted.kind == "accepted", accepted.model_text
        from codey.types.json_value import parse_json_object, require_json_string

        ref = require_json_string(parse_json_object(accepted.model_text)["task_ref"], "task_ref")
        child = runtime.app.durable.claim_task_checkpoint(ref)
        worker = threading.Thread(target=lambda: run_tool_task(runtime.app.durable, child,
            stores=runtime.app.task_submission_stores, signal=threading.Event()))
        worker.start()
        deadline = time.monotonic() + 6
        while True:
            try:
                tool_resources_for(runtime.app.durable).process(ref, child.attempt_token)
                break
            except ValueError:
                assert time.monotonic() < deadline, runtime.app.durable.task(ref)
                threading.Event().wait(0.02)
        newline = "\r" if tty and os.name == "nt" else "\n"
        from codey.operations.tool_tasks import control_tool_task
        from codey.policies.task_policy import TaskPolicy

        denied = control_tool_task(ToolCall("task_input", {"task_ref": ref, "data": "unauthorized"}),
            durable=runtime.app.durable, owner_task_id="r", store=runtime.app.managed_outputs,
            caller_policy=TaskPolicy(grants=frozenset({"control"})))
        assert not denied.ok and "original execution capability" in denied.model_text
        first = ToolCall("task_input", {"task_ref": ref, "data": "one" + newline})
        from codey.operations.kernel.kernel_execution import execute_turn

        if codemode:
            from codey.codemode.kernel import build_execute_turn_executor
            from codey.runtime.write.checkpoint import operation_from_checkpoint

            first = ToolCall("codemode", {"code": "return unwrap(await tools.task_input(inputs));", "inputs": first.args})
            state = operation_from_checkpoint(runtime.app.durable.task("r"), "s")
            assert state is not None
            runtime.app.runtime_mutations.advance_execution("s", "r", expected=state, leaf="writer_running")
            executor = build_execute_turn_executor(session=runtime.session, snapshot=runtime.snapshot,
                run_id="r", effect_scope="", turn=1, project_path=runtime.project, session_id="s",
                intent_sink=runtime.sink, durable=runtime.app.durable, managed_outputs=runtime.app.managed_outputs,
                workspace_revision_store=runtime.app.workspace_revisions)
            delivered = execute_turn(runtime.session, [first], run_id="r", turn=1, session_id="s",
                project_path=runtime.project, snapshot=runtime.snapshot, durable=runtime.app.durable,
                intent_sink=runtime.sink, managed_outputs=runtime.app.managed_outputs,
                workspace_revision_store=runtime.app.workspace_revisions, executors={"codemode": executor})[0]
        else:
            delivered = runtime.execute(first, turn=1)
        assert delivered.ok, delivered.model_text
        # Duplicate delivery does not begin a new model turn or advance the operation.
        repeated = execute_turn(runtime.session, [first], run_id="r", turn=1, session_id="s",
            project_path=runtime.project, snapshot=runtime.snapshot, durable=runtime.app.durable,
            intent_sink=runtime.sink, managed_outputs=runtime.app.managed_outputs,
            workspace_revision_store=runtime.app.workspace_revisions)[0]
        assert repeated.model_text == delivered.model_text
        second = runtime.execute(ToolCall("task_input", {"task_ref": ref, "data": "two" + newline,
                                                         "eof": not tty}), turn=2)
        assert second.ok, second.model_text
        worker.join(timeout=8)
        assert not worker.is_alive()
        observed = runtime.execute(ToolCall("task_output", {"task_ref": ref}), turn=3)
        payload = parse_json_object(observed.model_text)
        assert payload["outcome"] == "succeeded", observed.model_text
        assert len(runtime.app.durable.effects(ref)) == 1
        if tty:
            assert payload["streams_merged"] is True
        assert not runtime.execute(ToolCall("task_input", {"task_ref": ref, "data": "late"}), turn=4).ok
        with pytest.raises(ValueError, match="no longer available"):
            tool_resources_for(runtime.app.durable).process(ref, child.attempt_token)
    finally:
        if worker is not None and worker.is_alive():
            for child in runtime.app.durable.owned_tasks("r"):
                runtime.app.durable.request_cancel(child.task_id)
            worker.join(timeout=8)
        assert runtime.app.close()
