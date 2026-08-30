"""Prompt assembly metadata and fail-open trace recording.

Prompt envelopes describe model-visible prompt sections without owning provider
calls or changing prompt text. They are intentionally small: v1 is a local
assembly helper plus a trace sink, not a plugin system or prompt policy layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from codey.runtime import cancellation
from codey.workspace.context_epoch import (
    PROVIDER_TURN_ADMISSION,
    PROVIDER_TURN_BOUNDARY,
    context_epoch_id,
)


DEFAULT_PROMPT_SEPARATOR = "\n\n"
MODEL_BOUNDARY_FRESHNESS = frozenset((PROVIDER_TURN_BOUNDARY,))
MAX_PROMPT_SOURCE_REFS = 64
MAX_PROMPT_REF_CHARS = 160


@dataclass(frozen=True)
class PromptEnvelopeSection:
    name: str
    text: str
    purpose: str = ""
    model_visible: bool = True
    source_refs: tuple[str, ...] = ()
    budget: int = 0
    freshness: str = ""
    truncated: bool = False
    epoch_id: str = ""
    admission_reason: str = ""
    capability_id: str = ""


@dataclass(frozen=True)
class RenderedPromptSection:
    name: str
    text: str
    purpose: str
    model_visible: bool
    source_refs: tuple[str, ...]
    budget: int
    freshness: str
    truncated: bool
    epoch_id: str = ""
    admission_reason: str = ""
    capability_id: str = ""

    @property
    def rendered_length(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class RenderedPromptEnvelope:
    text: str
    sections: tuple[RenderedPromptSection, ...]
    separator: str = DEFAULT_PROMPT_SEPARATOR


class PromptEnvelope:
    def __init__(
        self,
        sections: Iterable[PromptEnvelopeSection] = (),
        *,
        separator: str = DEFAULT_PROMPT_SEPARATOR,
    ) -> None:
        self.separator = str(separator)
        self._sections = list(sections)

    def render(self) -> RenderedPromptEnvelope:
        rendered: list[RenderedPromptSection] = []
        for section in self._sections:
            text = str(section.text or "")
            if not text or not section.model_visible:
                continue
            rendered.append(RenderedPromptSection(
                name=str(section.name or "prompt_section"),
                text=text,
                purpose=str(section.purpose or ""),
                model_visible=bool(section.model_visible),
                source_refs=_source_refs(section.source_refs, section.name),
                budget=max(0, int(section.budget or 0)),
                freshness=str(section.freshness or ""),
                truncated=bool(section.truncated),
                epoch_id=str(section.epoch_id or ""),
                admission_reason=str(section.admission_reason or ""),
                capability_id=str(section.capability_id or ""),
            ))
        return RenderedPromptEnvelope(
            text=self.separator.join(section.text for section in rendered),
            sections=tuple(rendered),
            separator=self.separator,
        )


class FailOpenPromptTrace:
    """Small adapter around RunTraceRecorder-style objects.

    Trace failures must not affect provider execution. Cancellation still
    propagates so a trace hook cannot mask an explicit stop request.
    """

    def __init__(self, trace: Any | None) -> None:
        self.trace = trace

    def call(self, method: str, *args: Any, **kwargs: Any) -> None:
        if self.trace is None:
            return
        try:
            fn = getattr(self.trace, method, None)
            if callable(fn):
                fn(*args, **kwargs)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            if _is_trace_cancellation(exc):
                raise
            return

    def record_section(
        self,
        section: PromptEnvelopeSection | RenderedPromptSection,
        *,
        freshness_override: str = "",
    ) -> None:
        if self.trace is None:
            return
        try:
            if not getattr(section, "model_visible", True):
                return
            freshness = freshness_override or str(getattr(section, "freshness", "") or "")
            name = str(getattr(section, "name", "") or "prompt_section")
            text = str(getattr(section, "text", "") or "")
            budget = max(0, int(getattr(section, "budget", 0) or 0))
            truncated = bool(getattr(section, "truncated", False))
            source_refs = _source_refs(
                getattr(section, "source_refs", ()),
                getattr(section, "name", ""),
            )
            purpose = str(getattr(section, "purpose", "") or "")
            model_visible = bool(getattr(section, "model_visible", True))
            epoch_id = str(getattr(section, "epoch_id", "") or "")
            admission_reason = str(getattr(section, "admission_reason", "") or "")
            capability_id = str(getattr(section, "capability_id", "") or "")
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            if _is_trace_cancellation(exc):
                raise
            return
        # Admission metadata is appended only when present so legacy trace
        # sinks keep receiving the exact same keyword contract as before.
        admission_kwargs: dict[str, str] = {}
        if epoch_id:
            admission_kwargs["epoch_id"] = epoch_id
        if admission_reason:
            admission_kwargs["admission_reason"] = admission_reason
        if capability_id:
            admission_kwargs["capability_id"] = capability_id
        self.call(
            "record_prompt_section",
            name,
            text,
            budget=budget,
            truncated=truncated,
            freshness=freshness,
            source_refs=source_refs,
            purpose=purpose,
            model_visible=model_visible,
            **admission_kwargs,
        )

    def record_envelope(
        self,
        envelope: PromptEnvelope | RenderedPromptEnvelope,
        *,
        freshness_override: str = "",
    ) -> None:
        if self.trace is None:
            return
        rendered = envelope.render() if isinstance(envelope, PromptEnvelope) else envelope
        for section in rendered.sections:
            self.record_section(section, freshness_override=freshness_override)


def is_model_boundary_freshness(value: object) -> bool:
    return str(value or "").strip() in MODEL_BOUNDARY_FRESHNESS


def record_provider_send_prompt(
    trace: Any | None,
    *,
    name: str,
    text: str,
    purpose: str,
    source_ref: str,
    capability_id: str = "",
    epoch_id: str = "",
) -> None:
    """Record one outbound prompt at the safe provider-turn boundary.

    This is the single shared projection for "a prompt is about to be sent":
    it stamps the provider_send freshness, a content-addressed epoch id
    (overridable so a caller can share an epoch computed for the same bytes),
    and the fixed admission reason. It never changes the prompt text or the
    send.
    """
    FailOpenPromptTrace(trace).record_section(PromptEnvelopeSection(
        name=name,
        text=text,
        purpose=purpose,
        freshness=PROVIDER_TURN_BOUNDARY,
        source_refs=(source_ref,),
        epoch_id=epoch_id or context_epoch_id(text),
        admission_reason=PROVIDER_TURN_ADMISSION,
        capability_id=capability_id,
    ))


def _is_trace_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, (cancellation.TaskCancelled, cancellation.DeadlineExceeded))


def _source_refs(values: Iterable[object], fallback_name: object) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    refs: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = str(value or "").strip()
        if not text:
            continue
        text = text[:MAX_PROMPT_REF_CHARS]
        if text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= MAX_PROMPT_SOURCE_REFS:
            break
    if refs:
        return tuple(refs)
    name = _identifier(fallback_name, 80) or "prompt_section"
    return (f"prompt_section:{name}",)


def _identifier(value: object, limit: int) -> str:
    text = str(value or "").strip()[:limit]
    return "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)
