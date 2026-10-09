"""Affinity model: nodes, edges, hints, specs, identity, and payload rules.

Owns data shapes, stable identities, and field/payload constraints. Pure:
no Store, no sources, no events, no file locks, no atomic writes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from codey.ghost import _common
from codey.ghost._warnings import bounded_warnings
from codey.ghost.numbers import clamp_unit_float, coerce_unit_float
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text

AFFINITY_SCHEMA_VERSION = 1
MAX_AFFINITY_NODES = 500
MAX_AFFINITY_EDGES = 2_000
MAX_AFFINITY_EVENTS = 5_000
MAX_AFFINITY_STATE_BYTES = 1024 * 1024
MAX_AFFINITY_EVENTS_BYTES = 1024 * 1024
MAX_AFFINITY_REFS = 32
MAX_AFFINITY_REF_HASHES = 512
MAX_AFFINITY_HINT_REFS = 8
MAX_AFFINITY_WARNINGS = 20
MAX_EDGE_OUT_DEGREE = 16
NODE_LEARNING_RATE = 0.22
EDGE_LEARNING_RATE = 0.16
NODE_HALF_LIFE_DAYS = 90.0
EDGE_HALF_LIFE_DAYS = 120.0
MIN_NODE_WEIGHT = 0.04
MIN_EDGE_WEIGHT = 0.01
MAX_HINTS = 16
_STATE_KIND = "ghost_affinity_state_projection"
_NODE_SPEC_KEYS = frozenset(
    {"kind", "key", "label", "scope", "scope_ref", "confidence", "reward", "source_refs", "evidence_refs", "metadata"}
)
_EDGE_SPEC_KEYS = frozenset(
    {"source", "target", "relation", "scope", "scope_ref", "confidence", "reward", "source_refs", "proof_refs"}
)

AFFINITY_SCOPES = frozenset({"user", "project", "session"})
AFFINITY_NODE_KINDS = frozenset(
    {
        "user_preference",
        "project",
        "research_concept",
        "correction",
        "action_tendency",
        "provider_behavior",
        "task_type",
    }
)
AFFINITY_NODE_STATUSES = frozenset({"active", "expired", "superseded"})
AFFINITY_EDGE_RELATIONS = frozenset(
    {
        "associated_with",
        "prefers_for",
        "works_well_for",
        "struggles_with",
        "mentions_concept",
        "used_in_task",
    }
)
AFFINITY_EDGE_STATUSES = frozenset({"active", "expired"})
HINT_KINDS = frozenset(
    {
        "directive_order",
        "work_priority",
        "research_priority",
    }
)


@dataclass(frozen=True)
class AffinityNode:
    id: str
    kind: str
    key: str
    label: str
    scope: str
    scope_ref: str
    status: str
    weight: float
    confidence: float
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    source_ref_hashes: tuple[str, ...] = ()
    evidence_ref_hashes: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    last_reinforced_at: str = ""
    last_decayed_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "status": self.status,
            "weight": self.weight,
            "confidence": self.confidence,
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "source_ref_hashes": list(_bounded_ref_hashes(self.source_ref_hashes or self.source_refs)),
            "evidence_ref_hashes": list(_bounded_ref_hashes(self.evidence_ref_hashes or self.evidence_refs)),
            "metadata": _clean_metadata(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_decayed_at": self.last_decayed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> AffinityNode | None:
        if not isinstance(payload, Mapping):
            return None
        kind = _clean_node_kind(payload.get("kind"))
        scope = _clean_scope(payload.get("scope"))
        status = _clean_node_status(payload.get("status"))
        if not kind or not scope or not status:
            return None
        node_id = clip_signal_text(payload.get("id"), 120)
        key = _clean_key(payload.get("key"), 180)
        label = _clean_label(payload.get("label"), 180)
        if not node_id or not key or not label:
            return None
        weight = _unit_float_or_none(payload.get("weight"))
        confidence = _unit_float_or_none(payload.get("confidence"))
        if weight is None or confidence is None:
            return None
        return cls(
            id=node_id,
            kind=kind,
            key=key,
            label=label,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 120),
            status=status,
            weight=weight,
            confidence=confidence,
            source_refs=_bounded_refs(payload.get("source_refs")),
            evidence_refs=_bounded_refs(payload.get("evidence_refs")),
            source_ref_hashes=_bounded_ref_hashes(payload.get("source_ref_hashes") or payload.get("source_refs")),
            evidence_ref_hashes=_bounded_ref_hashes(payload.get("evidence_ref_hashes") or payload.get("evidence_refs")),
            metadata=_clean_metadata(payload.get("metadata")),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            last_reinforced_at=clip_signal_text(payload.get("last_reinforced_at"), 80),
            last_decayed_at=clip_signal_text(payload.get("last_decayed_at"), 80),
        )


@dataclass(frozen=True)
class AffinityEdge:
    id: str
    source: str
    target: str
    relation: str
    scope: str
    scope_ref: str
    status: str
    weight: float
    confidence: float
    source_refs: tuple[str, ...] = ()
    proof_refs: tuple[str, ...] = ()
    source_ref_hashes: tuple[str, ...] = ()
    proof_ref_hashes: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    last_reinforced_at: str = ""
    last_decayed_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "status": self.status,
            "weight": self.weight,
            "confidence": self.confidence,
            "source_refs": list(self.source_refs),
            "proof_refs": list(self.proof_refs),
            "source_ref_hashes": list(_bounded_ref_hashes(self.source_ref_hashes or self.source_refs)),
            "proof_ref_hashes": list(_bounded_ref_hashes(self.proof_ref_hashes or self.proof_refs)),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_decayed_at": self.last_decayed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> AffinityEdge | None:
        if not isinstance(payload, Mapping):
            return None
        relation = _clean_relation(payload.get("relation"))
        scope = _clean_scope(payload.get("scope"))
        status = _clean_edge_status(payload.get("status"))
        if not relation or not scope or not status:
            return None
        edge_id = clip_signal_text(payload.get("id"), 120)
        source = clip_signal_text(payload.get("source"), 120)
        target = clip_signal_text(payload.get("target"), 120)
        if not edge_id or not source or not target or source == target:
            return None
        weight = _unit_float_or_none(payload.get("weight"))
        confidence = _unit_float_or_none(payload.get("confidence"))
        if weight is None or confidence is None:
            return None
        return cls(
            id=edge_id,
            source=source,
            target=target,
            relation=relation,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 120),
            status=status,
            weight=weight,
            confidence=confidence,
            source_refs=_bounded_refs(payload.get("source_refs")),
            proof_refs=_bounded_refs(payload.get("proof_refs")),
            source_ref_hashes=_bounded_ref_hashes(payload.get("source_ref_hashes") or payload.get("source_refs")),
            proof_ref_hashes=_bounded_ref_hashes(payload.get("proof_ref_hashes") or payload.get("proof_refs")),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            last_reinforced_at=clip_signal_text(payload.get("last_reinforced_at"), 80),
            last_decayed_at=clip_signal_text(payload.get("last_decayed_at"), 80),
        )


@dataclass(frozen=True)
class AffinityHint:
    kind: str
    target: str
    confidence: float
    weight: float
    reason_code: str
    source_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target,
            "confidence": self.confidence,
            "weight": self.weight,
            "reason_code": self.reason_code,
            "source_refs": list(self.source_refs),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class GhostAffinitySyncResult:
    ok: bool
    skipped_reason: str = ""
    nodes_changed: int = 0
    edges_changed: int = 0
    total_nodes: int = 0
    total_edges: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AffinityNodeSpec:
    kind: str
    key: str
    label: str
    scope: str
    scope_ref: str
    confidence: float
    reward: float
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AffinityEdgeSpec:
    source: str
    target: str
    relation: str
    scope: str
    scope_ref: str
    confidence: float
    reward: float
    source_refs: tuple[str, ...]
    proof_refs: tuple[str, ...] = ()


def _node_id(kind: str, scope: str, scope_ref: str, key: str) -> str:
    raw = "|".join(
        (_clean_node_kind(kind), _clean_scope(scope), clip_signal_text(scope_ref, 120), _clean_key(key, 180))
    )
    return "gan_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _edge_id(source: str, target: str, relation: str, scope: str, scope_ref: str) -> str:
    clean_source = clip_signal_text(source, 120)
    clean_target = clip_signal_text(target, 120)
    clean_relation = _clean_relation(relation)
    if clean_relation == "associated_with":
        clean_source, clean_target = sorted((clean_source, clean_target))
    raw = "|".join((clean_source, clean_target, clean_relation, _clean_scope(scope), clip_signal_text(scope_ref, 120)))
    return "gae_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _node_spec_payload(spec: AffinityNodeSpec) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": _clean_node_kind(spec.kind),
        "key": _clean_key(spec.key, 180),
        "label": _clean_label(spec.label, 180),
        "scope": _clean_scope(spec.scope),
        "scope_ref": clip_signal_text(spec.scope_ref, 120),
        "confidence": _unit_float(spec.confidence),
        "reward": _unit_float(spec.reward),
        "source_refs": list(_bounded_refs(spec.source_refs)),
        "evidence_refs": list(_bounded_refs(spec.evidence_refs)),
        "metadata": _clean_metadata(spec.metadata),
    }
    if _node_spec_from_payload(payload) is None:
        raise ValueError("invalid affinity node reinforcement event")
    return payload


def _node_spec_from_payload(payload: object) -> AffinityNodeSpec | None:
    if not isinstance(payload, Mapping):
        return None
    kind = _clean_node_kind(payload.get("kind"))
    key = _clean_key(payload.get("key"), 180)
    label = _clean_label(payload.get("label"), 180)
    scope = _clean_scope(payload.get("scope"))
    scope_ref = clip_signal_text(payload.get("scope_ref"), 120)
    source_refs = _bounded_refs(payload.get("source_refs"))
    evidence_refs = _bounded_refs(payload.get("evidence_refs"))
    if not kind or not key or not label or not scope:
        return None
    if not source_refs and not evidence_refs:
        return None
    return AffinityNodeSpec(
        kind=kind,
        key=key,
        label=label,
        scope=scope,
        scope_ref=scope_ref,
        confidence=_unit_float(payload.get("confidence")),
        reward=_unit_float(payload.get("reward")),
        source_refs=source_refs,
        evidence_refs=evidence_refs,
        metadata=_clean_metadata(payload.get("metadata")),
    )


def _edge_spec_payload(spec: AffinityEdgeSpec) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": clip_signal_text(spec.source, 120),
        "target": clip_signal_text(spec.target, 120),
        "relation": _clean_relation(spec.relation),
        "scope": _clean_scope(spec.scope),
        "scope_ref": clip_signal_text(spec.scope_ref, 120),
        "confidence": _unit_float(spec.confidence),
        "reward": _unit_float(spec.reward),
        "source_refs": list(_bounded_refs(spec.source_refs)),
        "proof_refs": list(_bounded_refs(spec.proof_refs)),
    }
    if _edge_spec_from_payload(payload) is None:
        raise ValueError("invalid affinity edge reinforcement event")
    return payload


def _edge_spec_from_payload(payload: object) -> AffinityEdgeSpec | None:
    if not isinstance(payload, Mapping):
        return None
    source = clip_signal_text(payload.get("source"), 120)
    target = clip_signal_text(payload.get("target"), 120)
    relation = _clean_relation(payload.get("relation"))
    scope = _clean_scope(payload.get("scope"))
    scope_ref = clip_signal_text(payload.get("scope_ref"), 120)
    source_refs = _bounded_refs(payload.get("source_refs"))
    proof_refs = _bounded_refs(payload.get("proof_refs"))
    if not source or not target or source == target or not relation or not scope:
        return None
    if not source_refs and not proof_refs:
        return None
    return AffinityEdgeSpec(
        source=source,
        target=target,
        relation=relation,
        scope=scope,
        scope_ref=scope_ref,
        confidence=_unit_float(payload.get("confidence")),
        reward=_unit_float(payload.get("reward")),
        source_refs=source_refs,
        proof_refs=proof_refs,
    )


def _clean_node_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_NODE_KINDS else ""


def _clean_node_status(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_NODE_STATUSES else ""


def _clean_edge_status(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_EDGE_STATUSES else ""


def _clean_relation(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_EDGE_RELATIONS else ""


def _clean_scope(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_SCOPES else ""


def _clean_hint_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in HINT_KINDS else ""


def _clean_key(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = clip_signal_text(text, limit).strip().strip(".")
    if not text or contains_sensitive_signal_text(text):
        return ""
    return text


def _clean_label(value: object, limit: int = 180) -> str:
    text = _clean_key(value, limit)
    if not text:
        return ""
    lower = text.casefold()
    if "prompt" in lower or "raw" in lower or "source body" in lower:
        return ""
    return text


def _clean_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, object] = {}
    for key, item in value.items():
        clean_key = _clean_key(key, 80)
        if not clean_key:
            continue
        if isinstance(item, bool) or item is None:
            clean_item: object = item
        elif isinstance(item, int):
            clean_item = int(item)
        elif isinstance(item, float):
            clean_item = _unit_float(item)
        else:
            clean_item = _clean_key(item, 180)
        if isinstance(clean_item, str) and not clean_item:
            continue
        out[clean_key] = clean_item
        if len(out) >= 16:
            break
    return out


def _bounded_refs(values: object, *, limit: int = MAX_AFFINITY_REFS) -> tuple[str, ...]:
    return _merge_refs((), _list(values), limit=limit)


def _ref_hash(value: object) -> str:
    text = clip_signal_text(value, 180)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]


def _bounded_ref_hashes(values: object, *, limit: int = MAX_AFFINITY_REF_HASHES) -> tuple[str, ...]:
    return _merge_ref_hashes((), _list(values), limit=limit)


def _merge_ref_hashes(
    current: object,
    incoming: object,
    *,
    limit: int = MAX_AFFINITY_REF_HASHES,
) -> tuple[str, ...]:
    out: list[str] = []
    for value in (*_list(current), *_list(incoming)):
        text = clip_signal_text(value, 180)
        digest = text if len(text) == 24 and all(char in "0123456789abcdef" for char in text) else _ref_hash(text)
        if digest and digest not in out:
            out.append(digest)
    return tuple(out[-max(1, int(limit or 1)) :])


def _merge_refs(current: object, incoming: object, *, limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for value in (*_list(current), *_list(incoming)):
        text = clip_signal_text(value, 180)
        if not text or contains_sensitive_signal_text(text):
            continue
        if "\n" in text or "\r" in text or "\t" in text:
            continue
        if text not in out:
            out.append(text)
    return tuple(out[-max(1, int(limit or 1)) :])


def _bounded_warnings(values: object) -> tuple[str, ...]:
    return bounded_warnings(_list(values), limit=MAX_AFFINITY_WARNINGS)


def _valid_affinity_node_payload(payload: object) -> bool:
    node = AffinityNode.from_payload(payload)
    return node is not None and _common.strict_payload_equal(payload, node.to_payload())


def _valid_affinity_edge_payload(payload: object) -> bool:
    edge = AffinityEdge.from_payload(payload)
    return edge is not None and _common.strict_payload_equal(payload, edge.to_payload())


def _valid_node_spec_payload(payload: object) -> bool:
    spec = _node_spec_from_payload(payload)
    return (
        spec is not None
        and isinstance(payload, Mapping)
        and _common.mapping_keys_within(payload, _NODE_SPEC_KEYS)
        and _common.strict_payload_equal(payload, _node_spec_payload(spec))
    )


def _valid_edge_spec_payload(payload: object) -> bool:
    spec = _edge_spec_from_payload(payload)
    return (
        spec is not None
        and isinstance(payload, Mapping)
        and _common.mapping_keys_within(payload, _EDGE_SPEC_KEYS)
        and _common.strict_payload_equal(payload, _edge_spec_payload(spec))
    )


def _field(value: Any, name: str) -> object:
    from codey.ghost._common import field_value

    return field_value(value, name)


def _list(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, set):
        return tuple(value)
    return (value,)


def _unit_float_or_none(value: object) -> float | None:
    return coerce_unit_float(value, digits=6)


def _unit_float(value: object) -> float:
    return clamp_unit_float(value, digits=6)


__all__ = [
    "AFFINITY_EDGE_RELATIONS",
    "AFFINITY_EDGE_STATUSES",
    "AFFINITY_NODE_KINDS",
    "AFFINITY_NODE_STATUSES",
    "AFFINITY_SCHEMA_VERSION",
    "AFFINITY_SCOPES",
    "AffinityEdge",
    "AffinityEdgeSpec",
    "AffinityHint",
    "AffinityNode",
    "AffinityNodeSpec",
    "GhostAffinitySyncResult",
    "HINT_KINDS",
    "MAX_AFFINITY_EDGES",
    "MAX_AFFINITY_EVENTS",
    "MAX_AFFINITY_EVENTS_BYTES",
    "MAX_AFFINITY_HINT_REFS",
    "MAX_AFFINITY_NODES",
    "MAX_AFFINITY_REF_HASHES",
    "MAX_AFFINITY_REFS",
    "MAX_AFFINITY_STATE_BYTES",
    "MAX_AFFINITY_WARNINGS",
    "MAX_EDGE_OUT_DEGREE",
    "MAX_HINTS",
    "MIN_EDGE_WEIGHT",
    "MIN_NODE_WEIGHT",
    "EDGE_HALF_LIFE_DAYS",
    "EDGE_LEARNING_RATE",
    "NODE_HALF_LIFE_DAYS",
    "NODE_LEARNING_RATE",
]
