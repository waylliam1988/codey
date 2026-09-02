"""Turn-level tool execution orchestration and batch intent recording.

Decomposes tool execution in a turn into:
1. Planning & Intent Phase:
   - Evaluates policy and replay decisions for ALL calls in the turn.
   - Captures policy-denied, approval-required, intent-error, and effect-backed tools.
   - Records tool call intents early to obtain effect IDs.
   - Records the turn-level DeliveryBatchIntent before any execution starts,
     ensuring that durable recovery knows the full provider-visible envelope
     of the turn.
2. Execution Phase:
   - Emits tool started events.
   - Executes calls and records outcomes.
   - Records settlements in finally blocks.
   - Handles approval stops and cancellation gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from codey.agents.state import AgentLoopSession
from codey.agents.tool_execution import (
    TurnState,
    call_arg,
    emit_tool_started_after_intent,
    evaluate_tool_call_policy,
    execute_tool_call,
    mark_policy_denied_run,
    policy_denied,
    policy_error_outcome,
    record_tool_call_intent,
    record_tool_call_settlement,
    record_tool_outcome,
    request_shell_approval,
    tool_error_outcome,
)
from codey.runtime import cancellation
from codey.runtime.models import ToolCall
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    compute_batch_digest,
    new_batch_id,
)


@dataclass(frozen=True)
class PlannedToolCall:
    tool_index: int
    call: ToolCall
    effect_id: str = ""
    replay_decision: Any = None
    replay_class_str: str = "unsafe"
    policy_denied: bool = False
    policy_decision: Any = None
    requires_approval: bool = False
    intent_error: Exception | None = None


@dataclass(frozen=True)
class TurnToolExecutionResult:
    turn_state: TurnState
    stopped: bool = False
    stop_summary: str = ""
    stop_reason: str = ""


def execute_turn_tools(
    session: AgentLoopSession,
    calls: Sequence[ToolCall],
    *,
    turn: int,
) -> TurnToolExecutionResult:
    turn_state = TurnState()
    if not calls:
        return TurnToolExecutionResult(turn_state=turn_state)

    # 1. Planning & Intent Phase
    planned: list[PlannedToolCall] = []
    batch_items: list[DeliveryBatchItem] = []

    for tool_index, call in enumerate(calls):
        policy_decision, replay_decision = evaluate_tool_call_policy(
            session,
            call,
            turn=turn,
            tool_index=tool_index,
        )
        rclass_val = getattr(getattr(replay_decision, "replay_class", None), "value", "unsafe")
        if rclass_val not in {"safe", "unsafe"}:
            rclass_val = "safe" if getattr(replay_decision, "replay_class", None) == ReplayClass.SAFE else "unsafe"

        if policy_denied(policy_decision):
            planned.append(
                PlannedToolCall(
                    tool_index=tool_index,
                    call=call,
                    replay_class_str=rclass_val,
                    policy_denied=True,
                    policy_decision=policy_decision,
                    replay_decision=replay_decision,
                )
            )
            batch_items.append(
                DeliveryBatchItem(
                    tool_index=tool_index,
                    tool_name=call.name,
                    ref=f"denied:{call.name}:{tool_index}",
                    replay_class=rclass_val,
                    is_denied=True,
                )
            )
            continue

        if call.name == "shell":
            planned.append(
                PlannedToolCall(
                    tool_index=tool_index,
                    call=call,
                    requires_approval=True,
                    replay_class_str=rclass_val,
                    policy_decision=policy_decision,
                    replay_decision=replay_decision,
                )
            )
            batch_items.append(
                DeliveryBatchItem(
                    tool_index=tool_index,
                    tool_name=call.name,
                    ref=f"approval:{call.name}:{tool_index}",
                    replay_class=rclass_val,
                    is_denied=False,
                )
            )
            # Stop planning further calls after shell approval requirement
            break

        try:
            effect_id = record_tool_call_intent(
                session,
                call,
                turn=turn,
                tool_index=tool_index,
                replay_decision=replay_decision,
            )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            planned.append(
                PlannedToolCall(
                    tool_index=tool_index,
                    call=call,
                    replay_class_str=rclass_val,
                    intent_error=exc,
                    replay_decision=replay_decision,
                    policy_decision=policy_decision,
                )
            )
            batch_items.append(
                DeliveryBatchItem(
                    tool_index=tool_index,
                    tool_name=call.name,
                    ref=f"error:{call.name}:{tool_index}",
                    replay_class=rclass_val,
                    is_denied=True,
                )
            )
            continue

        planned.append(
            PlannedToolCall(
                tool_index=tool_index,
                call=call,
                effect_id=effect_id,
                replay_class_str=rclass_val,
                replay_decision=replay_decision,
                policy_decision=policy_decision,
            )
        )
        batch_items.append(
            DeliveryBatchItem(
                tool_index=tool_index,
                tool_name=call.name,
                ref=effect_id or f"synthetic:{call.name}:{tool_index}",
                replay_class=rclass_val,
                is_denied=False,
            )
        )

    # Record turn-level batch intent covering ALL planned results
    if batch_items and session.tool_result_delivery is not None and session.session_id and session.run_id:
        try:
            batch_id = new_batch_id(session.run_id, turn)
            items_tuple = tuple(batch_items)
            digest = compute_batch_digest(items_tuple)
            intent = DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=session.session_id,
                run_id=session.run_id,
                turn=turn,
                items=items_tuple,
                batch_digest=digest,
            )
            session.tool_result_delivery.record_batch_intent(
                session.session_id,
                session.run_id,
                intent,
            )
        except Exception:
            pass

    # 2. Execution Phase
    for item in planned:
        if item.policy_denied:
            outcome = policy_error_outcome(item.policy_decision)
            if item.call.name == "run":
                mark_policy_denied_run(session)
            record_tool_outcome(
                session,
                turn_state,
                turn=turn,
                call=item.call,
                outcome=outcome,
                tool_index=item.tool_index,
                effect_id="",
                replay_class=item.replay_class_str,
                is_denied=True,
            )
            continue

        if item.intent_error is not None:
            outcome = tool_error_outcome(item.intent_error)
            record_tool_outcome(
                session,
                turn_state,
                turn=turn,
                call=item.call,
                outcome=outcome,
                tool_index=item.tool_index,
                effect_id="",
                replay_class=item.replay_class_str,
                is_denied=True,
            )
            continue

        if item.requires_approval:
            path = call_arg(item.call, "path", ".")
            command = call_arg(item.call, "command").strip()
            request_shell_approval(
                session,
                path=path,
                command=command,
                policy_decision=item.policy_decision,
            )
            return TurnToolExecutionResult(
                turn_state=turn_state,
                stopped=True,
                stop_summary="shell command requires approval",
                stop_reason="approval",
            )

        effect_id = item.effect_id
        replay_decision = item.replay_decision
        emit_tool_started_after_intent(
            session,
            item.call,
            turn=turn,
            tool_index=item.tool_index,
        )
        try:
            try:
                outcome = execute_tool_call(
                    session,
                    item.call,
                    turn=turn,
                    tool_index=item.tool_index,
                )
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded) as exc:
                if effect_id:
                    record_tool_call_settlement(
                        session,
                        effect_id,
                        outcome=tool_error_outcome(exc),
                        replay_decision=replay_decision,
                    )
                raise
            except Exception as exc:
                outcome = tool_error_outcome(exc)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            outcome = tool_error_outcome(exc)

        try:
            record_tool_outcome(
                session,
                turn_state,
                turn=turn,
                call=item.call,
                outcome=outcome,
                tool_index=item.tool_index,
                effect_id=effect_id,
                replay_class=item.replay_class_str,
                is_denied=False,
            )
        finally:
            if effect_id:
                record_tool_call_settlement(
                    session,
                    effect_id,
                    outcome=outcome,
                    replay_decision=replay_decision,
                )

    return TurnToolExecutionResult(turn_state=turn_state)


__all__ = [
    "PlannedToolCall",
    "TurnToolExecutionResult",
    "execute_turn_tools",
]
