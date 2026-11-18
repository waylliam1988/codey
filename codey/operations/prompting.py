"""Prompt assembly and prompt-trace helpers for operations."""

from __future__ import annotations

from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, PromptEnvelopeSection


def record_secondary_input_prepared_trace(
    trace: object | None,
    phase: str,
    **sections: object,
) -> None:
    if trace is None:
        return
    sink = FailOpenPromptTrace(trace)
    phase_text = (phase or "secondary").strip() or "secondary"
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


__all__ = ["record_secondary_input_prepared_trace"]
