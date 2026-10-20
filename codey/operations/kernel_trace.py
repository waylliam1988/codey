"""Trace projections of actual shared-kernel turns; no execution decisions."""

from typing import Any

from codey.workspace.context_admission import GhostContextSource, admit_ghost_context
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt
from codey.utils.refs import digest_text


def record_outbound(
    trace: Any,
    prompt: str,
    *,
    native: bool,
    pending_reply: Any,
    pending_results: Any,
    ghost_sources: tuple[GhostContextSource, ...] = (),
    require_ghost_sources: bool = False,
    admit_hints: bool = True,
) -> None:
    if pending_reply is not None or (native and pending_results is not None):
        return
    admission = admit_ghost_context(
        prompt,
        ghost_sources,
        request_scope=(ghost_sources[0].scope if ghost_sources else ""),
        require_all=require_ghost_sources,
        admit_hints=admit_hints,
    )
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
