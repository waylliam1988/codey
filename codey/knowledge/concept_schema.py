"""Concept vocabulary shared by notes, tools, and the Concept Graph.

Concepts are virtual: they never become Markdown notes. This module owns
normalization and relation-cleaning rules so note.py and the Concept Graph
read model agree without importing each other (note.py -> concept_schema.py
only; the builder lives in concepts.py).
"""

from __future__ import annotations

import re

CONCEPT_EDGE_KINDS = ("affects", "uses", "causes", "part_of", "enables", "relates")
MAX_RELATIONS_PER_NOTE = 8
MAX_CONCEPT_LENGTH = 48

_MACHINE_TAGS = frozenset({"research"})
_MACHINE_TAG_PREFIXES = ("session:",)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_WS_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EDGE_PUNCT = " \t\"'`.,;:!?()[]{}<>"


def normalize_concept(value: object) -> str:
    """Return the canonical concept text, or "" when the value is noise.

    Noise: empty text, URLs, machine tags (research / session:*), bare years,
    and single Latin characters. Single CJK characters stay valid concepts.
    """
    text = _WS_RE.sub(" ", str(value or "").lower()).strip(_EDGE_PUNCT)
    if not text:
        return ""
    if "://" in text or text.startswith("www."):
        return ""
    if text in _MACHINE_TAGS or text.startswith(_MACHINE_TAG_PREFIXES):
        return ""
    if _YEAR_RE.match(text):
        return ""
    if len(text) < 2 and not _CJK_RE.search(text):
        return ""
    return text[:MAX_CONCEPT_LENGTH].strip(_EDGE_PUNCT)


def clean_relations(
    raw: object, *, limit: int = MAX_RELATIONS_PER_NOTE
) -> tuple[list[dict], list[str]]:
    """Normalize declared concept relations; return (relations, warnings).

    Lenient by design: bad items are dropped with a warning instead of
    failing the whole write. Type checking happens earlier in tool_contract.
    """
    if raw in (None, "", [], ()):
        return [], []
    if not isinstance(raw, (list, tuple)):
        return [], ["relations must be a list of objects"]
    relations: list[dict] = []
    warnings: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            warnings.append("dropped non-object relation")
            continue
        src = normalize_concept(item.get("src"))
        dst = normalize_concept(item.get("dst"))
        if not src or not dst:
            warnings.append("dropped relation with an empty or noisy concept")
            continue
        if src == dst:
            warnings.append(f"dropped self-relation on '{src}'")
            continue
        kind = str(item.get("kind") or "relates").strip().lower()
        if kind not in CONCEPT_EDGE_KINDS:
            kind = "relates"
        key = (src, dst, kind)
        if key in seen:
            continue
        seen.add(key)
        relations.append({"src": src, "dst": dst, "kind": kind})
    if len(relations) > limit:
        warnings.append(f"kept first {limit} of {len(relations)} relations")
        relations = relations[:limit]
    return relations, warnings


def concept_tags(tags: object) -> list[str]:
    """Normalized concept candidates from note tags (machine tags removed)."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags if isinstance(tags, (list, tuple)) else []:
        concept = normalize_concept(tag)
        if concept and concept not in seen:
            seen.add(concept)
            out.append(concept)
    return out
