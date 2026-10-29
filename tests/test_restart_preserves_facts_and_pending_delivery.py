"""Reconstruction is not delivery; every restart retains settled task facts."""
from __future__ import annotations

from types import SimpleNamespace

from codey.operations.completion_gate import evaluate
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.provider_actions import ProviderActions
from tests.test_session_log_receipt_recovery_preserves_facts import (
    _dirs,
    _gate_context,
    _recover_formal,
    _settle_edit_then_runs,
)


def test_second_restart_keeps_unknown_observation_blocking(tmp_path):
    project, state, logdir = _dirs(tmp_path)
    _, _, counts, _ = _settle_edit_then_runs(
        project, state, logdir, "s", "r", [{"exit_code": 0}, {}],
    )
    first, _, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    assert evaluate(first, "done", context=_gate_context(first)).complete is False
    second, _, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    assert evaluate(second, "done", context=_gate_context(second)).complete is False
    assert second.edited_files == first.edited_files
    assert second.verifications == first.verifications
    assert counts == {"edit": 1, "run": 2}


def test_restart_before_send_still_has_same_native_results(tmp_path):
    project, state, logdir = _dirs(tmp_path)
    _settle_edit_then_runs(project, state, logdir, "s", "r", [{"exit_code": 0}])
    _, first, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    _, second, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    assert second.recovered_tool_result_batch_id == first.recovered_tool_result_batch_id != ""
    # A settled safe effect is never replayed as a new delivery.  Its facts
    # remain available through the settled projection after a second restart.
    assert second.recovered_tool_outcomes == ()
    assert len(second.settled_tool_outcomes) == 2
    # The formal entry path must still deliver the pending settled receipt to
    # the provider; recovery facts and provider delivery are separate views.
    from codey.durable import DurableSession, SQLiteDurableStore
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    batch = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")[0]
    assert batch.is_recovered is True
    assert batch.is_terminal is False


def test_successful_send_closes_delivery_but_keeps_all_facts(tmp_path):
    project, state, logdir = _dirs(tmp_path)
    _, _, counts, _ = _settle_edit_then_runs(
        project, state, logdir, "s", "r", [{"exit_code": 0}],
    )
    first, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    from codey.durable import DurableSession, SQLiteDurableStore
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    sink = KernelEffectSink(
        RuntimeMutationLine(durable=durable), session_id="s", run_id="r", provider_id="web",
        recovered_batch_id=recovery.recovered_tool_result_batch_id,
            managed_outputs=ManagedOutputStore(state), durable=durable,
    )
    sends = []
    provider = SimpleNamespace(send=lambda text: sends.append(text) or "ack")
    assert ProviderActions(provider, sink).send("original results") == "ack"
    second, recovery2, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    assert recovery2.recovered_tool_outcomes == ()
    assert second.edited_files == first.edited_files
    assert second.verifications == first.verifications
    assert evaluate(second, "done", context=_gate_context(second)).complete is True
    assert sends == ["original results"]
    assert counts == {"edit": 1, "run": 1}


def test_project_adapter_restores_delivered_facts_without_resending(tmp_path):
    import json

    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    project, state, logdir = _dirs(tmp_path)
    _settle_edit_then_runs(project, state, logdir, "s", "r", [{"exit_code": 0}])
    _, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    from codey.durable import DurableSession, SQLiteDurableStore
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    sink = KernelEffectSink(
        RuntimeMutationLine(durable=durable), session_id="s", run_id="r", provider_id="web",
            recovered_batch_id=recovery.recovered_tool_result_batch_id, managed_outputs=ManagedOutputStore(state),
            durable=durable,
    )
    ProviderActions(SimpleNamespace(send=lambda _: "ack"), sink).send("results")
    _, after, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    sends = []
    provider = SimpleNamespace(send=lambda text: sends.append(text) or json.dumps({"tool": "done", "args": {"summary": "done"}}))
    from codey.workspace.revision import WorkspaceRevisionStore

    result = run(AgentRequest(
        provider=provider, project=project, task="fix a.py", fresh_chat=False,
        provider_id="web", run_id="r", project_changes_required=True,
        task_policy=_policy_for_project(), settled_tool_outcomes=after.settled_tool_outcomes,
        workspace_revision_store=WorkspaceRevisionStore(state),
    ))
    assert result.stop_reason == "done"
    assert len(result.facts.verifications) == 1
    assert len(sends) == 1


def _policy_for_project():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"}))


def test_real_entry_delivers_recovered_batch_before_done(tmp_path):
    import json
    from dataclasses import replace

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.context import RunWork
    from codey.operations.recovery import record_entry_policy
    from codey.operations.task_entry import run_entry_kernel
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.workspace.revision import WorkspaceRevisionStore
    from tests.test_chat_workspace_tools_and_answer_completion import workspace_frame

    project, state, logdir = _dirs(tmp_path)
    _settle_edit_then_runs(project, state, logdir, "s", "r", [{"exit_code": 0}])
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    mutations = RuntimeMutationLine(durable=durable)
    record_entry_policy(
        mutations,
        session_id="s",
        run_id="r",
        policy=_policy_for_project(),
        durable=durable,
    )
    fresh, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    prompts = []
    provider = SimpleNamespace(send=lambda text: prompts.append(text) or json.dumps({"tool": "done", "args": {"summary": "done"}}))
    frame = workspace_frame(project, provider, intent="project", task="fix a.py", project_changes_required=True)
    frame.run_id = "r"
    frame.fresh_chat = False
    frame.request = replace(frame.request, session_id="s", project=str(project), intent="project")
    frame.project_text = str(project)
    frame.recovered_tool_outcomes = recovery.recovered_tool_outcomes
    frame.settled_tool_outcomes = recovery.settled_tool_outcomes
    frame.recovered_tool_result_batch_id = recovery.recovered_tool_result_batch_id
    work = RunWork([], ExecutionEvidence(workspace_revision=fresh.workspace_revision,
                                         workspace_fingerprint=fresh.workspace_fingerprint))
    from codey.app.context import AppContext
    from codey.workspace.changes import collect_changes

    deps = SimpleNamespace(state=AppContext(state, start_scheduler=False), runtime_mutations=mutations,
                           workspace_revisions=WorkspaceRevisionStore(state), managed_outputs=ManagedOutputStore(state),
                           collect_changes=collect_changes)
    result = run_entry_kernel(frame, work, SimpleNamespace(on_event=lambda _: None, on_shell_request=None), deps,
                              task_kind="project")
    assert result.event["stop_reason"] == "done"
    assert len(prompts) == 1 and "run out" in prompts[0]
    batch = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")[0]
    assert batch.is_delivered is True
    assert len(frame.entry_session.verifications) == 1


def test_crash_after_entry_sink_initialization_keeps_delivery_pending(tmp_path):
    from codey.operations.task_entry import _entry_provider_sink
    project, state, logdir = _dirs(tmp_path)
    _settle_edit_then_runs(project, state, logdir, "s", "r", [{"exit_code": 0}])
    _, before, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    from codey.durable import DurableSession, SQLiteDurableStore
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    deps = SimpleNamespace(runtime_mutations=RuntimeMutationLine(durable=durable),
                           managed_outputs=ManagedOutputStore(state), state=SimpleNamespace(durable=durable))
    frame = SimpleNamespace(request=SimpleNamespace(session_id="s"), run_id="r", provider_id="local",
                            provider=SimpleNamespace(), recovered_tool_result_batch_id=before.recovered_tool_result_batch_id)
    _entry_provider_sink(frame, deps, "project")
    _, after, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    assert [row.effect_id for row in after.recovered_tool_outcomes] == [
        row.effect_id for row in before.recovered_tool_outcomes
    ]



def test_provider_change_after_acknowledged_delivery_can_resume(tmp_path):
    from codey.operations.task_entry import _entry_provider_sink
    from codey.runtime.core.operation_state import LEAF_WRITER_RUNNING, RuntimeOperationStore

    project, state, logdir = _dirs(tmp_path)
    _settle_edit_then_runs(project, state, logdir, "s", "r", [{"exit_code": 0}])
    _, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    from codey.durable import DurableSession, SQLiteDurableStore
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    mutations = RuntimeMutationLine(durable=durable)
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                            recovered_batch_id=recovery.recovered_tool_result_batch_id,
                                managed_outputs=ManagedOutputStore(state), durable=durable)
    ProviderActions(SimpleNamespace(send=lambda _: "ack"), sink).send("results")
    deps = SimpleNamespace(runtime_mutations=mutations, managed_outputs=ManagedOutputStore(state),
                           state=SimpleNamespace(durable=durable))
    frame = SimpleNamespace(request=SimpleNamespace(session_id="s"), run_id="r", provider_id="web",
                            provider=SimpleNamespace(), recovered_tool_result_batch_id="")
    _entry_provider_sink(frame, deps, "project")
    current = RuntimeOperationStore(durable=durable).load("s", "r")
    assert current.leaf == LEAF_WRITER_RUNNING
    assert current.provider_id == "web"
    _, after, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
    assert after.recovered_tool_outcomes == ()
    assert len(after.settled_tool_outcomes) == 2
