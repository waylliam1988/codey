"""Prepare an unsent kernel turn once; received turns reuse their original input."""
from dataclasses import dataclass
from typing import Any

from codey.operations import kernel_prompt, kernel_protocol
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import TaskSession


@dataclass(frozen=True)
class PreparedKernelTurn:
    snapshot: TurnSnapshot
    prompt: str


def prepare_kernel_turn(session: TaskSession, *, user_task: str, context_text: str,
                        task_guidance: str, native: bool, completion_context: Any = None) -> PreparedKernelTurn:
    snapshot = kernel_protocol.build_turn_snapshot(session, native=native)
    prompt = kernel_prompt.kernel_prompt_for_session(
        session, user_task=user_task, contract_text=snapshot.contract_text,
        context_text=context_text, controller_allowed=snapshot.allowed,
        native=native, tool_names=snapshot.tool_names, task_guidance=task_guidance,
        coding_context=prepare_coding_context(session, completion_context=completion_context),
    )
    return PreparedKernelTurn(snapshot, prompt)
