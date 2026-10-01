"""Bounded exhaustive traces checked against independent task/delivery facts.

3 initial verification histories x every length-3 trace over restart,
acknowledged send and failed send. This checks real durable adapters;
it is not a proof for unbounded executions or external providers.
"""
from itertools import product
from types import SimpleNamespace

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
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
            sink = KernelEffectSink(
                RuntimeMutationLine(RuntimeSessionLog(logdir)), session_id="s", run_id="r",
                provider_id="web", recovered_batch_id=recovery.recovered_tool_result_batch_id,
                managed_outputs=ManagedOutputStore(state),
            )
            def send(text, action=action):
                if action == "failure":
                    raise RuntimeError("provider send failed")
                return "ack"
            provider = KernelRecordedProvider(SimpleNamespace(send=send), sink)
            if action == "failure":
                with pytest.raises(RuntimeError, match="provider send failed"):
                    provider.send("original results")
            else:
                assert provider.send("original results") == "ack"
            # After an attempted send, ambiguity forbids automatic redelivery.
            expected_pending = False
        restored, recovery, _, _, _ = _recover_formal(project, state, logdir, "s", "r")
        assert bool(recovery.recovered_tool_outcomes) is expected_pending
        assert restored.edited_files == expected_edits
        assert restored.verifications == expected_checks
        assert evaluate(restored, "done", context=_gate_context(restored)).complete is (exits[-1] == 0)
        assert counts == {"edit": 1, "run": len(exits)}
        assert (project / "a.py").read_text(encoding="utf-8") == "x = 2\n"
