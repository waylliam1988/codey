"""Static provider capability hints used only for conservative fallback ordering."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Literal

from codey.providers.ids import normalize_provider_id


ProviderFit = Literal["ok", "avoid"]
Reliability = Literal["high", "medium", "low"]

FIT_OK: ProviderFit = "ok"
FIT_AVOID: ProviderFit = "avoid"

RELIABILITY_HIGH: Reliability = "high"
RELIABILITY_MEDIUM: Reliability = "medium"
RELIABILITY_LOW: Reliability = "low"


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    json_reliability: Reliability
    coding_fit: ProviderFit
    research_fit: ProviderFit
    review_fit: ProviderFit
    context_budget_hint: int
    native_tool_interference_risk: Reliability
    needs_canary_by_default: bool
    failure_families: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


DEFAULT_PROVIDER_CAPABILITY = ProviderCapability(
    provider_id="default",
    json_reliability=RELIABILITY_MEDIUM,
    coding_fit=FIT_OK,
    research_fit=FIT_OK,
    review_fit=FIT_OK,
    context_budget_hint=12000,
    native_tool_interference_risk=RELIABILITY_MEDIUM,
    needs_canary_by_default=False,
    failure_families=(),
    notes=("default static provider capability",),
)


PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    "deepseek": ProviderCapability(
        provider_id="deepseek",
        json_reliability=RELIABILITY_MEDIUM,
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
        context_budget_hint=16000,
        native_tool_interference_risk=RELIABILITY_MEDIUM,
        needs_canary_by_default=False,
        failure_families=("response_missing", "submission_uncertain"),
        notes=("default web provider",),
    ),
    "mimo": ProviderCapability(
        provider_id="mimo",
        json_reliability=RELIABILITY_MEDIUM,
        coding_fit=FIT_OK,
        research_fit=FIT_AVOID,
        review_fit=FIT_OK,
        context_budget_hint=12000,
        native_tool_interference_risk=RELIABILITY_MEDIUM,
        needs_canary_by_default=False,
        failure_families=("response_missing",),
        notes=("kept as fallback for Research when no better sibling is available",),
    ),
    "stepfun": ProviderCapability(
        provider_id="stepfun",
        json_reliability=RELIABILITY_MEDIUM,
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
        context_budget_hint=12000,
        native_tool_interference_risk=RELIABILITY_MEDIUM,
        needs_canary_by_default=False,
        failure_families=("response_missing", "submission_uncertain"),
        notes=("stable sibling fallback",),
    ),
    "qwen": ProviderCapability(
        provider_id="qwen",
        json_reliability=RELIABILITY_MEDIUM,
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
        context_budget_hint=12000,
        native_tool_interference_risk=RELIABILITY_HIGH,
        needs_canary_by_default=False,
        failure_families=("control_missing", "readiness_stale"),
        notes=("web provider with more native-tool interference risk",),
    ),
    "glm": ProviderCapability(
        provider_id="glm",
        json_reliability=RELIABILITY_MEDIUM,
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
        context_budget_hint=12000,
        native_tool_interference_risk=RELIABILITY_MEDIUM,
        needs_canary_by_default=False,
        failure_families=("response_missing",),
        notes=("web provider",),
    ),
    "local": ProviderCapability(
        provider_id="local",
        json_reliability=RELIABILITY_HIGH,
        coding_fit=FIT_OK,
        research_fit=FIT_OK,
        review_fit=FIT_OK,
        context_budget_hint=16000,
        native_tool_interference_risk=RELIABILITY_LOW,
        needs_canary_by_default=False,
        failure_families=("transient",),
        notes=("OpenAI-compatible local endpoint",),
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
    "RELIABILITY_HIGH",
    "RELIABILITY_LOW",
    "RELIABILITY_MEDIUM",
    "Reliability",
    "capability_for",
    "rank_providers",
]
