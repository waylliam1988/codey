"""Durable receipts for oversized research web/source outputs.

Extracted from the former research execution loop to keep receipt handling
separate from task execution. Duck-typed store only: this module never imports
``codey.storage.managed_outputs`` (or ``codey.toolchain.runtime``), so the
research/runtime import boundary stays intact. ``None`` store means
clip-only. Never changes execution semantics.
"""

from __future__ import annotations

from typing import Any

RESEARCH_OUTPUT_BUDGET_BYTES = 24_000
RESEARCH_OUTPUT_HEAD_BYTES = 12_000
RESEARCH_OUTPUT_TAIL_BYTES = 8_000


class _Outcome:
    def __init__(self, model_text: str, *, ok: bool, presentation_result: str = "") -> None:
        self.model_text = model_text
        self.ok = ok
        self.status = "ok" if ok else "error"
        self.exit_code = None
        self.changed = False
        self.truncated = False
        self.presentation = {"status": self.status, "result": (presentation_result or self.first_model_line(200))[:200]}
        self.audit: dict[str, object] = {}
        self.canonical: dict[str, object] = {}

    def first_model_line(self, limit: int) -> str:
        return next(iter(self.model_text.splitlines()), "")[:limit]


def head_tail_clip(text: str) -> tuple[str, int]:
    raw = text.encode("utf-8")
    raw_bytes = len(raw)
    head = raw[:RESEARCH_OUTPUT_HEAD_BYTES].decode("utf-8", errors="ignore")
    tail = (
        raw[-RESEARCH_OUTPUT_TAIL_BYTES:].decode("utf-8", errors="ignore")
        if len(raw) > RESEARCH_OUTPUT_HEAD_BYTES
        else ""
    )
    receipt = (
        f"\n[... output externalized: {raw_bytes} bytes; showing head/tail. "
        "Use narrower offsets for the rest.]\n"
    )
    return (head + receipt + tail if tail else head + receipt), raw_bytes


def display_ref_for_call(call: Any) -> str:
    try:
        args = getattr(call, "args", {}) or {}
        for key in ("url", "query", "id", "src"):
            value = str(args.get(key) or "").strip()
            if value:
                return value[:240]
    except Exception:
        pass
    return str(getattr(call, "name", "") or "")[:80]


def maybe_externalize_output(
    *,
    store: Any,
    session_id: str,
    run_id: str,
    permission_profile: str,
    call: Any,
    output: str,
    ok: bool,
    turn: int,
    tool_index: int,
    presentation_result: str = "",
    model_text_override: str = "",
) -> _Outcome:
    """Bound one web/source output, persisting a durable receipt when possible.

    ``output`` is the full text reserved for the receipt; when
    ``model_text_override`` is given the model sees only that window while
    the store keeps the full text.
    """
    text = str(output or "")
    model_text = str(model_text_override or text)
    if len(text.encode("utf-8")) <= RESEARCH_OUTPUT_BUDGET_BYTES:
        return _Outcome(model_text, ok=ok, presentation_result=presentation_result)
    audit: dict[str, object] = {}
    if store is not None and session_id and run_id:
        try:
            ref = store.write_tool_output(
                session_id=session_id,
                run_id=run_id,
                tool_id=f"{turn}:{tool_index}",
                permission_profile=permission_profile,
                tool_name=str(getattr(call, "name", "") or ""),
                display_ref=display_ref_for_call(call),
                text=text,
            )
        except Exception:
            ref = None
        if ref is None:
            audit["managed_output_failed"] = True
        else:
            audit["managed_output"] = {
                "handle": ref.handle,
                "original_bytes": ref.original_bytes,
                "stored_bytes": ref.stored_bytes,
                "sha256": ref.sha256,
                "original_sha256": ref.original_sha256,
                "stored_truncated": ref.stored_truncated,
            }
    # Window wins when the caller supplies one: the model must never see the
    # full-text head/tail. ToolResult appends the managed-output footer itself.
    if model_text_override:
        visible = model_text
    else:
        visible, _ = head_tail_clip(text)
    outcome = _Outcome(visible, ok=ok, presentation_result=presentation_result)
    outcome.truncated = True
    outcome.audit.update(audit)
    outcome.audit["externalized"] = True
    return outcome


__all__ = [
    "RESEARCH_OUTPUT_BUDGET_BYTES",
    "RESEARCH_OUTPUT_HEAD_BYTES",
    "RESEARCH_OUTPUT_TAIL_BYTES",
    "display_ref_for_call",
    "head_tail_clip",
    "maybe_externalize_output",
]
