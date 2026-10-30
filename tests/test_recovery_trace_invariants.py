"""Bounded exhaustive traces checked against independent task/delivery facts.

3 initial verification histories x every length-3 trace over restart,
acknowledged send and failed send. This checks real durable adapters;
it is not a proof for unbounded executions or external providers.
"""
from itertools import product
from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.completion_gate import evaluate
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.provider_actions import ProviderActions
from tests.test_session_log_receipt_recovery_preserves_facts import (
    _dirs,
    _gate_context,
    _recover_formal,
    _settle_edit_then_runs,
)


@pytest.mark.parametrize("exits", [(0,), (0, None), (0, None, 0)])
@pytest.mark.parametrize("trace", list(product(("restart", "ack", "failure"), repeat=3)))
def test_recovery_preserves_facts_and_never_reexecutes(tmp_path, exits, trace):
    project, state, logdir = _dirs(tmp_path)
    original, _, counts, _ = _settle_edit_then_runs(
        project, state, logdir, "s", "r", [{"exit_code": code} for code in exits],
    )
    expected_edits = dict(original.edited_files)
    expected_checks = list(original.verifications)
    expected_pending = True
    for action in trace:
        restored, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
        if action != "restart" and expected_pending:
            durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
            try:
                sink = KernelEffectSink(
                    RuntimeMutationLine(durable=durable),
                    session_id="s", run_id="r", provider_id="local",
                    recovered_batch_id=recovery.recovered_tool_result_batch_id,
                    managed_outputs=ManagedOutputStore(state), durable=durable,
                )
                from codey.operations.kernel_protocol import build_turn_snapshot

                # Direct storage-boundary sends still need the exact original
                # request identity that KernelContext.freeze supplies in production.
                sink.bind_request(build_turn_snapshot(restored), logical_turn=1)

                def send(text, action=action):
                    if action == "failure":
                        raise RuntimeError("provider send failed")
                    return "ack"

                provider = ProviderActions(SimpleNamespace(send=send), sink)
                if action == "failure":
                    with pytest.raises(RuntimeError, match="provider send failed"):
                        provider.send("original results")
                    # The provider receipt is ``maybe_sent``.  Recovery seals
                    # the unknown task instead of redelivering its batch.
                    from tests.support.receipt_fixtures import inspect_receipt_facts

                    result = inspect_receipt_facts(
                        SimpleNamespace(
                            state=SimpleNamespace(durable=durable),
                            runtime_mutations=RuntimeMutationLine(durable=durable),
                            runtime_effects=None,
                            tool_result_delivery=None,
                        ),
                        session_id="s", run_id="r", project=str(project), task_kind="project",
                    )
                    assert result.ok is False
                    assert durable.task("r").phase == "running"
                    assert any(effect.settlement.get("sent_state") == "maybe_sent"
                               for effect in durable.effects("r") if effect.settlement is not None)
                    durable.close()
                    assert counts == {"edit": 1, "run": len(exits)}
                    return
                else:
                    assert provider.send("original results") == "ack"
                    from codey.operations.generation_boundary import (
                        GenerationBoundaryRequest,
                        durable_generation_boundary_callback,
                    )
                    durable_generation_boundary_callback(durable=durable, session_id="s", task_id="r", sink=sink)(
                        GenerationBoundaryRequest("settle"))
            finally:
                durable.close()
            # After an attempted send, ambiguity forbids automatic redelivery.
            expected_pending = False
        restored, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
        assert bool(recovery.recovered_tool_outcomes or recovery.recovered_tool_result_batch_id) is expected_pending
        assert restored.edited_files == expected_edits
        assert restored.verifications == expected_checks
        assert evaluate(restored, "done", context=_gate_context(restored)).complete is (exits[-1] == 0)
        assert counts == {"edit": 1, "run": len(exits)}
        assert (project / "a.py").read_text(encoding="utf-8") == "x = 2\n"
