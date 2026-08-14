"""Named prompt context rendering with bounded fail-open behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from codey import cancellation
from codey.text_budget import clip_middle

FAILURE_POLICY_OMIT = "omit"
FAILURE_POLICY_RAISE = "raise"
FAILURE_POLICIES = frozenset((FAILURE_POLICY_OMIT, FAILURE_POLICY_RAISE))


@dataclass(frozen=True)
class ContextSource:
    key: str
    loader: Callable[[], str]
    budget: int
    freshness: str
    why_included: str
    failure_policy: str = FAILURE_POLICY_OMIT
    heading: str = ""


@dataclass(frozen=True)
class RenderedContextSource:
    key: str
    text: str
    budget: int
    truncated: bool
    freshness: str
    why_included: str


@dataclass(frozen=True)
class RenderedContextSources:
    text: str
    sources: tuple[RenderedContextSource, ...]


def render_context_source(source: ContextSource) -> RenderedContextSource | None:
    """Render one source, omitting empty or failed optional context."""
    if source.failure_policy not in FAILURE_POLICIES:
        raise ValueError(f"unknown context source failure policy: {source.failure_policy}")

    try:
        body = source.loader()
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        if source.failure_policy == FAILURE_POLICY_RAISE:
            raise
        return None

    text = str(body or "").strip()
    if not text:
        return None

    rendered = _render_block(source.heading, text)
    budget = max(0, int(source.budget or 0))
    if budget <= 0:
        return None
    rendered, truncated = clip_middle(rendered, budget)
    if not rendered:
        return None

    return RenderedContextSource(
        key=source.key,
        text=rendered,
        budget=budget,
        truncated=truncated,
        freshness=source.freshness,
        why_included=source.why_included,
    )


def render_context_sources(sources: Iterable[ContextSource]) -> str:
    """Render sources in order, separated by a single blank line."""
    return render_context_sources_with_metadata(sources).text


def render_context_sources_with_metadata(
    sources: Iterable[ContextSource],
) -> RenderedContextSources:
    """Render sources and keep metadata for bounded run trace manifests."""

    rendered = tuple(
        source
        for source in (render_context_source(item) for item in sources)
        if source is not None and source.text
    )
    return RenderedContextSources(
        text="\n\n".join(source.text for source in rendered),
        sources=rendered,
    )


def _render_block(heading: str, text: str) -> str:
    heading = str(heading or "").strip()
    if not heading:
        return text
    return f"{heading}\n{text}"
