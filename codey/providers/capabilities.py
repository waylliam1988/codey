"""Static provider capability hints used only for conservative fallback ordering."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal

from codey.providers.ids import normalize_provider_id

ProviderFit = Literal["ok", "avoid"]

FIT_OK: ProviderFit = "ok"
FIT_AVOID: ProviderFit = "avoid"


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    coding_fit: ProviderFit
    research_fit: ProviderFit
    review_fit: ProviderFit
    supports_native_tools: bool = False
    native_tools_default: bool = False
    context_window_tokens: int = 200_000
    context_reserve_tokens: int = 16_384
    context_keep_recent_tokens: int = 20_000


DEFAULT_PROVIDER_CAPABILITY = ProviderCapability(
    provider_id="default",
    coding_fit=FIT_OK,
    research_fit=FIT_OK,
    review_fit=FIT_OK,
)


PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    "deepseek": ProviderCapability(
        provider_id="deepseek",
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
    ),
    "mimo": ProviderCapability(
        provider_id="mimo",
        coding_fit=FIT_OK,
        research_fit=FIT_AVOID,
        review_fit=FIT_OK,
    ),
    "stepfun": ProviderCapability(
        provider_id="stepfun",
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
    ),
    "qwen": ProviderCapability(
        provider_id="qwen",
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
    ),
    "glm": ProviderCapability(
        provider_id="glm",
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
    ),
    "local": ProviderCapability(
        provider_id="local",
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
        supports_native_tools=True,
        native_tools_default=True,
        context_window_tokens=32_768,
        context_reserve_tokens=8_192,
        context_keep_recent_tokens=12_000,
    ),
}


def capability_for(provider_id: str) -> ProviderCapability:
    normalized = _provider_id(provider_id)
    capability = PROVIDER_CAPABILITIES.get(normalized)
    if capability is not None:
        return capability
    return replace(DEFAULT_PROVIDER_CAPABILITY, provider_id=normalized or "default")


def rank_providers(
    provider_ids: Iterable[str],
    mode: str,
    *,
    preferred: str = "",
    excluded: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return provider ids ordered by static fit while preserving input ties."""
    blocked = {_provider_id(item) for item in excluded}
    ordered = []
    seen: set[str] = set()
    for raw in provider_ids:
        provider_id = _provider_id(raw)
        if not provider_id or provider_id in blocked or provider_id in seen:
            continue
        seen.add(provider_id)
        ordered.append(provider_id)

    preferred_id = _provider_id(preferred)
    if preferred_id and preferred_id in seen:
        ordered = [preferred_id] + [
            provider_id for provider_id in ordered if provider_id != preferred_id
        ]
        # The explicit provider is already in front; only rank fallback siblings.
        return (preferred_id,) + _rank_without_preferred(
            ordered[1:],
            mode,
        )

    return _rank_without_preferred(ordered, mode)


def _rank_without_preferred(provider_ids: list[str], mode: str) -> tuple[str, ...]:
    return tuple(
        provider_id
        for _score, _index, provider_id in sorted(
            (
                (_fit_score(_fit_for_mode(capability_for(provider_id), mode)), index, provider_id)
                for index, provider_id in enumerate(provider_ids)
            )
        )
    )


def _fit_for_mode(capability: ProviderCapability, mode: str) -> ProviderFit:
    normalized = str(mode or "").strip().lower()
    if normalized == "research":
        return capability.research_fit
    if normalized in {"project", "coding"}:
        return capability.coding_fit
    if normalized == "hybrid":
        return _strictest_fit(capability.research_fit, capability.coding_fit)
    if normalized == "review":
        return capability.review_fit
    return FIT_OK


def _strictest_fit(*fits: ProviderFit) -> ProviderFit:
    return FIT_AVOID if FIT_AVOID in fits else FIT_OK


def _fit_score(fit: ProviderFit) -> int:
    if fit == FIT_AVOID:
        return 1
    return 0


def _provider_id(value: object) -> str:
    return normalize_provider_id(value)


__all__ = [
    "DEFAULT_PROVIDER_CAPABILITY",
    "FIT_AVOID",
    "FIT_OK",
    "PROVIDER_CAPABILITIES",
    "ProviderCapability",
    "ProviderFit",
    "capability_for",
    "rank_providers",
]
