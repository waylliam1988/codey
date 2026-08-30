"""Prompt assembly and prompt-trace helpers for operations."""

from __future__ import annotations

from typing import Any

from codey.runtime.prompt_envelope import FailOpenPromptTrace, PromptEnvelopeSection


def prepend_ghost_directive(prompt: str, directive: str) -> str:
    text = str(directive or "").strip()
    if not text:
        return prompt
    return f"{text}\n\n{prompt}"


def owner_prompt_with_ghost_directive(owner_prompt: str, directive: str) -> str:
    text = str(directive or "").strip()
    existing = str(owner_prompt or "").strip()
    if text and existing:
        return f"{text}\n\n{existing}"
    return text or existing


def join_local_contexts(*values: str) -> str:
    return "\n\n".join(str(value or "").strip() for value in values if str(value or "").strip())


def record_local_context_trace(trace: Any | None, *contexts: Any) -> None:
    if trace is None:
        return
    refs: list[dict[str, object]] = []
    for context in contexts:
        for node in getattr(context, "selected_nodes", ()) or ():
            refs.append({
                "id": getattr(node, "id", ""),
                "scope": getattr(node, "scope", ""),
                "kind": getattr(node, "kind", ""),
                "source": "local_context",
            })
        for item in getattr(context, "selected_items", ()) or ():
            refs.append({
                "id": getattr(item, "id", ""),
                "scope": getattr(item, "scope", ""),
                "kind": getattr(item, "kind", ""),
                "source": getattr(item, "source", "continuity"),
            })
    if not refs:
        return
    FailOpenPromptTrace(trace).call("record_local_context_refs", refs)


def record_secondary_input_prepared_trace(
    trace: Any | None,
    phase: str,
    **sections: object,
) -> None:
    if trace is None:
        return
    sink = FailOpenPromptTrace(trace)
    phase_text = str(phase or "secondary").strip() or "secondary"
    for name, text in sections.items():
        if not str(text or ""):
            continue
        sink.record_section(PromptEnvelopeSection(
            name=f"{phase_text}_{name}",
            text=str(text or ""),
            purpose=f"{phase_text} secondary input prepared",
            freshness="secondary_input_prepared",
            source_refs=(f"secondary_input:{phase_text}:{name}",),
        ))


__all__ = [
    "join_local_contexts",
    "owner_prompt_with_ghost_directive",
    "prepend_ghost_directive",
    "record_local_context_trace",
    "record_secondary_input_prepared_trace",
]
