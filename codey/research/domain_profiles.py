"""Evidence-standard profiles for Research tasks.

A profile is a small vector of evidence expectations: how fresh, how primary,
how much counterevidence, and whether data-backed conclusions need local
analysis. It answers "what kind of evidence makes a claim in this kind of
task more credible" and never answers "is this conclusion true".

Design rules locked by tests:

- Profiles are data, not rules: no planner logic, no connector access, no
  provider/router/permission awareness, and no I/O of any kind.
- Cross-domain tasks compose at runtime via ``merge_profiles``; there are no
  combination profiles (``finance_legal``), no inheritance, and no special
  cases. Complexity stays ``O(profiles x dimensions)``, not exponential.
- Unknown labels fall back to the ``general`` profile with a warning instead
  of guessing. Domain inference from free text is deliberately absent; it
  would be an experiment capability with its own A/B.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

GENERAL_PROFILE_ID = "general"
BUILTIN_PROFILE_IDS = (
    "finance",
    "general",
    "legal",
    "market",
    "science",
    "software_research",
)
MAX_MERGE_PROFILES = 4
MAX_PROFILE_KINDS = 8

# Ranked dimensions: later values are stricter. Merging takes the strictest.
FRESHNESS_ORDER = ("low", "medium", "high", "critical")
QUALITY_FLOOR_ORDER = ("any", "secondary", "primary")
PREFERENCE_ORDER = ("allowed", "preferred", "required")
REQUIREMENT_ORDER = ("none", "preferred", "required")
ANALYSIS_ORDER = ("optional", "preferred", "required")

# Connector-kind vocabulary the dry-run planner may map onto shipped
# connectors. Profiles name kinds, never concrete providers or connectors.
CONNECTOR_KINDS = frozenset({"data", "local", "news", "official", "paper"})


@dataclass(frozen=True)
class EvidenceProfile:
    """One evidence-standard vector. Expectations only, never judgments."""

    profile_id: str
    freshness_expectation: str = "low"
    source_quality_threshold: str = "any"
    primary_source_preference: str = "allowed"
    counterevidence_requirement: str = "none"
    analysis_for_data_claims: str = "optional"
    preferred_source_kinds: tuple[str, ...] = ()
    disfavored_source_kinds: tuple[str, ...] = ()
    preferred_connector_kinds: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            # "+" is the runtime composition marker; sanitizing it away
            # would make merged profiles look like builtin combination
            # names (finance_legal), which never exist by design.
            "profile_id": _payload_profile_id(self.profile_id),
            "freshness_expectation": _ranked(
                self.freshness_expectation, FRESHNESS_ORDER, "low"
            ),
            "source_quality_threshold": _ranked(
                self.source_quality_threshold, QUALITY_FLOOR_ORDER, "any"
            ),
            "primary_source_preference": _ranked(
                self.primary_source_preference, PREFERENCE_ORDER, "allowed"
            ),
            "counterevidence_requirement": _ranked(
                self.counterevidence_requirement, REQUIREMENT_ORDER, "none"
            ),
            "analysis_for_data_claims": _ranked(
                self.analysis_for_data_claims, ANALYSIS_ORDER, "optional"
            ),
            "preferred_source_kinds": list(_bounded_tokens(
                self.preferred_source_kinds, MAX_PROFILE_KINDS
            )),
            "disfavored_source_kinds": list(_bounded_tokens(
                self.disfavored_source_kinds, MAX_PROFILE_KINDS
            )),
            "preferred_connector_kinds": list(_bounded_tokens(
                self.preferred_connector_kinds, MAX_PROFILE_KINDS
            )),
            "warnings": list(_bounded_tokens(self.warnings, MAX_PROFILE_KINDS, width=120)),
        }


# The six atomic profiles from the roadmap. Every field is an expectation
# vector entry; none of them reference another profile or embed domain
# knowledge such as specific sites, databases, or search terms.
GENERAL_PROFILE = EvidenceProfile(
    profile_id="general",
    freshness_expectation="low",
    source_quality_threshold="any",
    primary_source_preference="allowed",
    counterevidence_requirement="preferred",
)

FINANCE_PROFILE = EvidenceProfile(
    profile_id="finance",
    freshness_expectation="high",
    source_quality_threshold="secondary",
    primary_source_preference="preferred",
    counterevidence_requirement="preferred",
    analysis_for_data_claims="required",
    preferred_source_kinds=("filing", "dataset", "official"),
    disfavored_source_kinds=("forum", "social", "aggregator"),
)

LEGAL_PROFILE = EvidenceProfile(
    profile_id="legal",
    freshness_expectation="high",
    source_quality_threshold="primary",
    primary_source_preference="required",
    counterevidence_requirement="preferred",
    analysis_for_data_claims="optional",
    preferred_source_kinds=("official", "filing", "standard"),
    disfavored_source_kinds=("forum", "social", "aggregator"),
)

MARKET_PROFILE = EvidenceProfile(
    profile_id="market",
    freshness_expectation="critical",
    source_quality_threshold="secondary",
    primary_source_preference="preferred",
    counterevidence_requirement="preferred",
    analysis_for_data_claims="required",
    preferred_source_kinds=("news", "filing", "dataset"),
    disfavored_source_kinds=("social", "aggregator"),
)

SCIENCE_PROFILE = EvidenceProfile(
    profile_id="science",
    freshness_expectation="medium",
    source_quality_threshold="secondary",
    primary_source_preference="preferred",
    counterevidence_requirement="required",
    analysis_for_data_claims="preferred",
    preferred_source_kinds=("peer_reviewed", "preprint", "dataset"),
    disfavored_source_kinds=("forum", "social", "aggregator"),
    preferred_connector_kinds=("paper", "data"),
)

SOFTWARE_RESEARCH_PROFILE = EvidenceProfile(
    profile_id="software_research",
    freshness_expectation="high",
    source_quality_threshold="secondary",
    primary_source_preference="preferred",
    counterevidence_requirement="preferred",
    analysis_for_data_claims="optional",
    preferred_source_kinds=("repository", "release", "issue", "official"),
    disfavored_source_kinds=("forum", "social", "aggregator"),
    preferred_connector_kinds=("local", "paper"),
)

BUILTIN_PROFILES = {
    profile.profile_id: profile
    for profile in (
        GENERAL_PROFILE,
        FINANCE_PROFILE,
        LEGAL_PROFILE,
        MARKET_PROFILE,
        SCIENCE_PROFILE,
        SOFTWARE_RESEARCH_PROFILE,
    )
}


def resolve_profile(labels: Iterable[object] = ()) -> EvidenceProfile:
    """Resolve task labels into one evidence profile.

    Deterministic and bounded: known labels merge in first-seen order capped
    at ``MAX_MERGE_PROFILES``; unknown labels fall back to ``general`` with a
    warning instead of guessing.
    """

    profiles: list[EvidenceProfile] = []
    seen: set[str] = set()
    unknown_label = False
    truncated = False
    for label in labels or ():
        profile_id = _profile_id(label)
        if not profile_id:
            continue
        profile = BUILTIN_PROFILES.get(profile_id)
        if profile is None:
            unknown_label = True
            continue
        if profile.profile_id in seen:
            continue
        if len(profiles) >= MAX_MERGE_PROFILES:
            truncated = True
            continue
        seen.add(profile.profile_id)
        profiles.append(profile)
    resolved = merge_profiles(*profiles) if profiles else GENERAL_PROFILE
    extra_warnings: tuple[str, ...] = ()
    if unknown_label:
        extra_warnings += ("unknown_profile_label",)
    if truncated:
        extra_warnings += ("profile_merge_truncated",)
    if extra_warnings:
        resolved = replace(
            resolved,
            warnings=_merge_tokens((resolved.warnings, extra_warnings)),
        )
    return resolved


def merge_profiles(*profiles: EvidenceProfile) -> EvidenceProfile:
    """Compose profiles per dimension. Data combination, never inheritance."""

    rows: list[EvidenceProfile] = []
    seen_segments: set[str] = set()
    warning_groups: list[tuple[str, ...]] = []
    truncated = False
    for profile in profiles or ():
        if not isinstance(profile, EvidenceProfile):
            continue
        warning_groups.append(profile.warnings)
        atoms = _profile_atoms(profile)
        if not atoms:
            continue
        for atom in atoms:
            if atom.profile_id in seen_segments:
                continue
            if len(rows) >= MAX_MERGE_PROFILES:
                truncated = True
                continue
            seen_segments.add(atom.profile_id)
            rows.append(atom)
    if not rows:
        return GENERAL_PROFILE
    segments = [row.profile_id for row in rows]
    warnings = _merge_tokens(tuple(warning_groups))
    if truncated:
        warnings = _merge_tokens((warnings, ("profile_merge_truncated",)))
    profile_id = "+".join(segments)
    if len(rows) == 1 and len(segments) == 1:
        single = rows[0]
        return single if warnings == single.warnings else replace(single, warnings=warnings)
    return EvidenceProfile(
        profile_id=profile_id,
        freshness_expectation=_strictest(
            (item.freshness_expectation for item in rows), FRESHNESS_ORDER
        ),
        source_quality_threshold=_strictest(
            (item.source_quality_threshold for item in rows), QUALITY_FLOOR_ORDER
        ),
        primary_source_preference=_strictest(
            (item.primary_source_preference for item in rows), PREFERENCE_ORDER
        ),
        counterevidence_requirement=_strictest(
            (item.counterevidence_requirement for item in rows), REQUIREMENT_ORDER
        ),
        analysis_for_data_claims=_strictest(
            (item.analysis_for_data_claims for item in rows), ANALYSIS_ORDER
        ),
        preferred_source_kinds=_union_tuples(
            tuple(item.preferred_source_kinds for item in rows)
        ),
        disfavored_source_kinds=_union_tuples(
            tuple(item.disfavored_source_kinds for item in rows)
        ),
        preferred_connector_kinds=_union_tuples(
            tuple(item.preferred_connector_kinds for item in rows)
        ),
        warnings=warnings,
    )


def _profile_atoms(profile: EvidenceProfile) -> tuple[EvidenceProfile, ...]:
    """Return sanitized atomic rows from a possibly composed profile id."""

    segments = tuple(
        part
        for part in (
            _profile_id(part) for part in str(profile.profile_id or "").split("+")
        )
        if part
    )
    if not segments:
        return ()
    if len(segments) == 1:
        segment = segments[0]
        if segment == profile.profile_id:
            return (profile,)
        return (replace(profile, profile_id=segment),)
    rows: list[EvidenceProfile] = []
    for segment in segments:
        rows.append(BUILTIN_PROFILES.get(segment) or replace(profile, profile_id=segment))
    return tuple(rows)


def _strictest(values: Iterable[str], order: tuple[str, ...]) -> str:
    best = order[0]
    for value in values:
        text = _ranked(value, order, order[0])
        if order.index(text) > order.index(best):
            best = text
    return best


def _union_tuples(groups: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    # Sorted so composing A+B and B+A yields byte-identical expectations;
    # builtin profiles keep their authored display order.
    return tuple(sorted(_merge_tokens(groups)))


def _merge_tokens(groups: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    out: list[str] = []
    for group in groups:
        for token in group:
            text = str(token or "").strip()
            if text and text not in out:
                out.append(text)
    return tuple(out)


def _bounded_tokens(values: Iterable[object], limit: int, width: int = 80) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if len(text) > max(0, int(width)):
            text = text[: width - 3].rstrip() + "..."
        if text and text not in out:
            out.append(text)
        if len(out) >= max(0, int(limit)):
            break
    return tuple(out)


def _ranked(value: object, order: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().casefold()
    return text if text in order else default


def _profile_id(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in text.replace(" ", "_")
    )
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")[:80]


def _payload_profile_id(value: object) -> str:
    parts = [_profile_id(part) for part in str(value or "").split("+")]
    return "+".join(part for part in parts if part)


__all__ = [
    "ANALYSIS_ORDER",
    "BUILTIN_PROFILES",
    "BUILTIN_PROFILE_IDS",
    "CONNECTOR_KINDS",
    "FRESHNESS_ORDER",
    "GENERAL_PROFILE",
    "GENERAL_PROFILE_ID",
    "MAX_MERGE_PROFILES",
    "PREFERENCE_ORDER",
    "QUALITY_FLOOR_ORDER",
    "REQUIREMENT_ORDER",
    "EvidenceProfile",
    "merge_profiles",
    "resolve_profile",
]
