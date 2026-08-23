"""Deterministic source-trust projection for Research sources.

Source trust answers exactly one question: "what kind of source is this,
objectively?" It projects the facts a source already carries (host suffix,
declared quality level/kind/freshness) onto a small, stable class taxonomy.
It never judges whether a claim is true, never fetches anything, never reads
page bodies, and never deletes or filters evidence -- consumers may only turn
projections into warnings, preferences, or threshold hints.

The projection and the domain profile are orthogonal: profiles state what a
task needs (``codey.research.domain_profiles``), projections state what a
source is. Combining both is the consumer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from codey.refs import clip as _clip
from codey.refs import identifier as _identifier
from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref


# Low-dimensional, cross-domain source classes. Concrete sites/databases are
# deliberately not classes; they would turn this into a domain knowledge base.
SOURCE_CLASSES = (
    "official",
    "primary",
    "peer_reviewed",
    "preprint",
    "dataset",
    "filing",
    "standard",
    "repository",
    "issue",
    "release",
    "news",
    "secondary",
    "forum",
    "social",
    "aggregator",
    "unknown",
)
WEAK_SOURCE_CLASSES = frozenset({"forum", "social", "aggregator", "unknown"})
STRONG_SOURCE_CLASSES = frozenset({
    "official",
    "primary",
    "peer_reviewed",
    "dataset",
    "filing",
    "standard",
})
TIER_WEAK = 1
TIER_MIDDLE = 2
TIER_STRONG = 3
SOURCE_CLASS_TIERS = {
    **{name: TIER_STRONG for name in STRONG_SOURCE_CLASSES},
    "preprint": TIER_MIDDLE,
    "repository": TIER_MIDDLE,
    "issue": TIER_MIDDLE,
    "release": TIER_MIDDLE,
    "news": TIER_MIDDLE,
    "secondary": TIER_MIDDLE,
    **{name: TIER_WEAK for name in WEAK_SOURCE_CLASSES},
}
MAX_PROJECTIONS = 24
MAX_SOURCE_CLASSES = 3
FRESHNESS_VALUES = frozenset({"fresh", "stale", "undated"})

_PREPRINT_HOSTS = ("arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com")
_PEER_REVIEWED_HOSTS = (
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "doi.org",
    "ieeexplore.ieee.org",
    "sciencedirect.com",
    "link.springer.com",
    "nature.com",
    "science.org",
    "dl.acm.org",
    "jmlr.org",
    "plos.org",
    "frontiersin.org",
)
_REPO_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
_FILING_HOSTS = ("sec.gov", "finra.org", "edi.gov")
_STANDARD_HOSTS = ("iso.org", "ieee.org", "ietf.org", "ansi.org", "w3.org", "itu.int")
_NEWS_HOST_MARKERS = ("reuters", "apnews", "bbc", "bloomberg", "nytimes", "wsj", "ft.com", "news")
_FORUM_HOSTS = (
    "reddit.com",
    "stackoverflow.com",
    "stackexchange.com",
    "quora.com",
    "zhihu.com",
    "v2ex.com",
    "news.ycombinator.com",
)
_SOCIAL_HOSTS = ("twitter.com", "x.com", "facebook.com", "weibo.com", "linkedin.com")

_KIND_CLASSES = {
    "official": "official",
    "data": "dataset",
    "media": "news",
    "blog": "secondary",
    "forum": "forum",
    "social": "social",
}


@dataclass(frozen=True)
class SourceTrustProjection:
    """Bounded read model of one source's objective properties.

    No raw text by design: refs, host, allow-listed classes, tier, and
    declared quality tokens only.
    """

    source_ref: str
    host: str = ""
    source_class: str = "unknown"
    classes: tuple[str, ...] = ()
    tier: int = TIER_WEAK
    freshness: str = "undated"
    level: str = ""
    kind: str = ""
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_ref": self.source_ref,
            "host": _clip(self.host, 120),
            "source_class": _class_token(self.source_class),
            "classes": [
                token for token in (_class_token(item) for item in self.classes[:MAX_SOURCE_CLASSES]) if token
            ],
            "tier": int(self.tier),
            "freshness": _identifier(self.freshness, 20) or "undated",
        }
        if self.level:
            payload["level"] = _clip(self.level, 20)
        if self.kind:
            payload["kind"] = _clip(self.kind, 20)
        if self.warnings:
            payload["warnings"] = list(_bounded_tokens(self.warnings, 4))
        return payload


def project_source_trust(source: object) -> SourceTrustProjection | None:
    """Project one source mapping/object into trust facts.

    Returns None when the source has neither a valid runtime ref nor any
    usable identity, so callers can drop half facts instead of guessing.
    """

    if isinstance(source, Mapping):
        get = source.get
    else:
        get = lambda name, default=None: getattr(source, name, default)  # noqa: E731
    raw_id = str(get("source_id") or "")
    ref = _normalize_runtime_ref(raw_id, kind="source")
    host = _host_of(get("host"), get("final_url_ref"), get("requested_url_ref"))
    if not ref:
        return None
    quality = get("quality")
    level = ""
    kind = ""
    freshness = ""
    if isinstance(quality, Mapping):
        level = _identifier(quality.get("level"), 20).lower()
        kind = _identifier(quality.get("kind"), 20).lower()
        freshness = _identifier(quality.get("freshness"), 20).lower()
    classes = _classify(host=host, kind=kind, level=level)
    primary_class = classes[0] if classes else "unknown"
    warnings: tuple[str, ...] = ()
    if SOURCE_CLASS_TIERS.get(primary_class, TIER_WEAK) == TIER_WEAK:
        warnings += ("weak_source_class",)
    return SourceTrustProjection(
        source_ref=ref,
        host=_clip(host, 120),
        source_class=primary_class,
        classes=classes or ("unknown",),
        tier=SOURCE_CLASS_TIERS.get(primary_class, TIER_WEAK),
        freshness=freshness if freshness in FRESHNESS_VALUES else "undated",
        level=_clip(level, 20),
        kind=_clip(kind, 20),
        warnings=warnings,
    )


def project_source_set(sources: Iterable[object]) -> tuple[SourceTrustProjection, ...]:
    """Project a bounded set of sources, skipping unusable rows."""

    out: list[SourceTrustProjection] = []
    seen: set[str] = set()
    for source in sources or ():
        if len(out) >= MAX_PROJECTIONS:
            break
        projection = project_source_trust(source)
        if projection is None or projection.source_ref in seen:
            continue
        seen.add(projection.source_ref)
        out.append(projection)
    return tuple(out)


def source_trust_warnings(sources: Mapping[str, Mapping[str, object]]) -> tuple[str, ...]:
    """Aggregate warnings over a record's source map.

    Moved verbatim from ``proof_quality`` so the proof review keeps emitting
    byte-identical warnings while the rules live beside the projection that
    explains them.
    """

    warnings: list[str] = []
    if len(sources) < 2:
        warnings.append("single_source")
    freshness = []
    levels = []
    kinds = []
    for source in sources.values():
        quality = source.get("quality")
        if isinstance(quality, Mapping):
            freshness.append(_identifier(quality.get("freshness"), 80))
            levels.append(_identifier(quality.get("level"), 80))
            kinds.append(_identifier(quality.get("kind"), 80))
    if freshness and all(item in {"", "undated", "stale"} for item in freshness):
        warnings.append("sources_stale_or_undated")
    if levels and not any(item == "primary" for item in levels):
        warnings.append("no_primary_source")
    if any(item in {"blog", "forum", "social"} for item in kinds):
        warnings.append("weak_source_kind")
    return tuple(dict.fromkeys(warnings))


def evaluate_against_profile(
    projections: Iterable[SourceTrustProjection],
    *,
    floor_tier: int = TIER_WEAK,
) -> dict[str, object]:
    """Combine projections with an optional strictness floor.

    Pure aggregation: counts, per-source below-floor warnings, and bounded
    preference hints. Never removes sources; rows below the floor stay in the
    set with a warning attached to the evaluation, not deleted.
    """

    rows = [item for item in projections or () if isinstance(item, SourceTrustProjection)]
    counts: dict[str, int] = {}
    for cls in SOURCE_CLASSES:
        matched = sum(1 for item in rows if item.source_class == cls)
        if matched:
            counts[cls] = matched
    warnings: list[str] = []
    if len(rows) < 2:
        warnings.append("single_source")
    if rows and all(item.freshness in {"", "undated", "stale"} for item in rows):
        warnings.append("sources_stale_or_undated")
    if rows and not any(item.tier >= TIER_STRONG for item in rows):
        warnings.append("no_strong_source")
    below_floor = sum(1 for item in rows if item.tier < max(TIER_WEAK, int(floor_tier)))
    if below_floor:
        warnings.append("sources_below_quality_threshold")
    weak = sum(1 for item in rows if item.tier == TIER_WEAK)
    if weak:
        warnings.append("weak_source_class_present")
    return {
        "count": len(rows),
        "class_counts": counts,
        "below_floor_count": below_floor,
        "warnings": warnings[:8],
    }


def _classify(*, host: str, kind: str, level: str) -> tuple[str, ...]:
    classes: list[str] = []

    def add(cls: str) -> None:
        token = _class_token(cls)
        if token and token not in classes and len(classes) < MAX_SOURCE_CLASSES:
            classes.append(token)

    if host:
        lowered = host.lower()
        if any(lowered == item or lowered.endswith("." + item) for item in _PREPRINT_HOSTS):
            add("preprint")
        if any(lowered == item or lowered.endswith("." + item) for item in _PEER_REVIEWED_HOSTS):
            add("peer_reviewed")
        if any(lowered == item or lowered.endswith("." + item) for item in _REPO_HOSTS):
            add("repository")
        if any(lowered == item or lowered.endswith("." + item) for item in _STANDARD_HOSTS):
            add("standard")
        if any(lowered == item or lowered.endswith("." + item) for item in _FILING_HOSTS):
            add("filing")
        if lowered.endswith(".gov") or ".gov." in lowered or lowered.endswith(".mil"):
            add("official")
        elif lowered.endswith(".edu") or ".edu." in lowered:
            add("primary")
        if any(marker in lowered for marker in _FORUM_HOSTS):
            add("forum")
        elif any(marker in lowered for marker in _SOCIAL_HOSTS):
            add("social")
        elif any(marker in lowered for marker in _NEWS_HOST_MARKERS):
            add("news")
    mapped = _KIND_CLASSES.get(kind, "")
    if mapped:
        add(mapped)
    if not classes:
        if level == "primary":
            add("primary")
        elif level == "secondary":
            add("secondary")
        else:
            add("unknown")
    return tuple(classes)


def _class_token(value: object) -> str:
    text = _identifier(value, 40).lower()
    return text if text in SOURCE_CLASSES else ""


def _bounded_tokens(values: Iterable[object], limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= max(0, int(limit)):
            break
    return tuple(out)


def _host_of(host: object, *url_refs: object) -> str:
    text = str(host or "").strip().lower().removeprefix("www.")
    if text:
        return text
    for ref in url_refs:
        if isinstance(ref, Mapping):
            candidate = str(ref.get("host") or "").strip().lower().removeprefix("www.")
            if candidate:
                return candidate
    return ""


__all__ = [
    "MAX_PROJECTIONS",
    "SOURCE_CLASSES",
    "SOURCE_CLASS_TIERS",
    "STRONG_SOURCE_CLASSES",
    "TIER_MIDDLE",
    "TIER_STRONG",
    "TIER_WEAK",
    "WEAK_SOURCE_CLASSES",
    "SourceTrustProjection",
    "evaluate_against_profile",
    "project_source_set",
    "project_source_trust",
    "source_trust_warnings",
]
