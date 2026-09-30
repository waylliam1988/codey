"""Trace projections of actual shared-kernel turns; no execution decisions."""

from typing import Any

from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.utils.refs import digest_text


def record_turn(trace: Any, *, phase: str, turn: int, snapshot: Any, plan: Any, native: bool) -> None:
    if trace is None:
        return
    sink = FailOpenPromptTrace(trace)
    contract_hash = digest_text(snapshot.contract_text)
    sink.call("record_protocol_codec", "native_openai" if native else "task_json", phase=phase,
              model_tool_contract_hash=contract_hash, runtime_tool_contract_hash=contract_hash)
    sink.call("record_tool_contract_hash", contract_hash, phase=phase)
    sink.call("record_runtime_tool_contract_hash", contract_hash, phase=phase)
    if plan.protocol_error:
        sink.call("record_protocol_error", plan.protocol_error_kind, phase=phase,
                  turn=turn, tool_name=plan.protocol_tool_name)
    else:
        sink.call("record_protocol_valid_turn", turn, phase=phase)
    sink.call("flush")
