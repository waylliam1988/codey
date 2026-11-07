"""Provider transport for the shared task kernel.

Provider capability selection, sending, native call-id receipts, and budget
drain. Failures raise explicit errors; the task loop maps them to results.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from codey.operations.task_session import TaskSession
from codey.providers.base import ProviderToolResult
from codey.runtime.core.models import ToolResult


def provider_uses_native(provider: object, *, provider_id: object = "") -> bool:
    """Reuse the production native-tool decision; never bare hasattr checks.

    The protocol is decided once at task start. An identification failure
    raises instead of silently falling back to web: continuing on the wrong
    protocol would mis-deliver native call ids.
    """
    from codey.providers.compat_transport import unwrap_provider
    from codey.providers.native_tools import supports_native_tools

    return supports_native_tools(unwrap_provider(provider), str(provider_id or ""))


def _native_tool_results(
    results: Sequence[ToolResult],
    session: TaskSession,
    *,
    boundary_prompt: str = "",
    result_context: Callable[[ToolResult], str] | None = None,
) -> list[ProviderToolResult]:
    from codey.operations.kernel_prompt import _result_context

    rows = list(results or [])
    ids = [row.call.call_id for row in rows]
    has_id = [bool(i) for i in ids]
    if rows and any(has_id) and not all(has_id):
        # Mixed batches would silently drop the valid call ids if we fell
        # back to text; fail closed instead. Cross-provider text delivery
        # only happens in the explicit provider_session_changed branch.
        raise ValueError(
            "mixed native batch: some results lack call ids; "
            "refusing to deliver a partial native chain"
        )
    messages: list[ProviderToolResult] = []
    for result in rows:
        call_id = result.call.call_id
        if not call_id:
            raise ValueError("native result has no original call id")
        content = result_context(result) if result_context else _result_context(result, session)
        messages.append(ProviderToolResult(call_id, content))
    if boundary_prompt and messages:
        last = messages[-1]
        messages[-1] = ProviderToolResult(last.call_id, last.content, user_content=boundary_prompt)
    return messages
