"""Trace projections of actual shared-kernel turns; no execution decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt
from codey.utils.refs import digest_text
from codey.workspace.context_admission import ContextAdmissionResult

if TYPE_CHECKING:
    from codey.operations.kernel.kernel_protocol import TurnSnapshot
    from codey.runs.trace import RunTraceRecorder
    from codey.runtime.core.models import ToolPlan


def record_outbound(
    trace: object | None,
    prompt: str,
    *,
    admission: ContextAdmissionResult,
) -> None:
    if trace is not None and admission.accepted:
        FailOpenPromptTrace(trace).call(
            "record_context_sources",
            admission.accepted,
            epoch_id=admission.epoch.epoch_id,
            admission_reason="provider_turn_boundary",
        )
    record_provider_send_prompt(
        trace, name="workspace_outbound_prompt", text=prompt, purpose="shared workspace kernel request",
        source_ref="provider_send:workspace", capability_id="agent_runner",
        epoch_id=admission.epoch.epoch_id,
    )


def record_turn(trace: RunTraceRecorder | None, *, phase: str, turn: int, snapshot: TurnSnapshot,
                plan: ToolPlan, native: bool) -> None:
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
        sink.call("record_protocol_valid_turn", turn, phase=phase, alias_rewrite_count=plan.alias_rewrite_count,
                  arg_repair_counts=plan.arg_repair_counts)
    sink.call("flush")
