"""A matching payload digest cannot authorize malformed or conflicting status."""
import json
from dataclasses import replace

import pytest

from codey.operations.kernel_receipts import restore_result_receipt, text_digest
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log


@pytest.mark.parametrize("corruption", ["missing", None, 0, 1, "false", True])
def test_failure_receipt_rejects_malformed_or_success_status_even_with_matching_digest(tmp_path, corruption):
    log, mutations = _new_log(tmp_path)
    _accept_and_mark(mutations, "s", "r", str(tmp_path))
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local")
    call = ToolCall("read", {"path": "a.txt"}, "c1")
    sink.begin_turn([("e1", call, 0)], turn=1)
    sink.settle("e1", False, result=ToolResult(call, "refused", ok=False))
    projection = RuntimeEffectStore(log).load_effects("s", "r")[0]
    data = json.loads(projection.settlement.result_payload)
    if corruption == "missing":
        del data["ok"]
    else:
        data["ok"] = corruption
    payload = json.dumps(data)
    modified = replace(projection, settlement=replace(projection.settlement,
        result_payload=payload, result_payload_sha256=text_digest(payload)))
    with pytest.raises(ValueError, match="fields|status"):
        restore_result_receipt(modified, store=None, session_id="s", run_id="r")
