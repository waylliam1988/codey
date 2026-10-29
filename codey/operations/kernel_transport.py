"""Provider transport for the shared task kernel.

Provider capability selection, sending, native call-id receipts, and budget
drain. Failures raise explicit errors; the task loop maps them to results.
"""

from __future__ import annotations

from typing import Any

from codey.providers.base import ProviderToolResult


def provider_uses_native(provider: Any, *, provider_id: object = "") -> bool:
    """Reuse the production native-tool decision; never bare hasattr checks.

    The protocol is decided once at task start. An identification failure
    raises instead of silently falling back to web: continuing on the wrong
    protocol would mis-deliver native call ids.
    """
    from codey.operations.provider_session import ProviderAdapter
    from codey.providers.native_tools import supports_native_tools

    probe = provider
    seen: set[int] = set()
    while isinstance(probe, ProviderAdapter):
        if id(probe) in seen:
            raise ValueError("cyclic provider adapter chain")
        seen.add(id(probe))
        probe = probe.provider
    return supports_native_tools(probe, str(provider_id or ""))


def _native_tool_results(
    results: Any,
    session: Any,
    *,
    boundary_prompt: str = "",
) -> list[ProviderToolResult]:
    from codey.operations.kernel_prompt import _result_context

    rows = list(results or [])
    ids = [str(getattr(getattr(r, "call", None), "call_id", "") or "") for r in rows]
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
        call_id = str(getattr(result.call, "call_id", "") or "")
        if not call_id:
            raise ValueError("native result has no original call id")
        messages.append(ProviderToolResult(call_id, _result_context(result, session)))
    if boundary_prompt and messages:
        last = messages[-1]
        messages[-1] = ProviderToolResult(last.call_id, last.content, user_content=boundary_prompt)
    return messages
