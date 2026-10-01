"""Affinity sources: hebbian/work/research/provider conversions.

Owns multi-source spec generation from already-loaded projections. Pure
conversions: no file locks, no persistence, no model calls. The affinity
store owns transactions and delegates here.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from codey.ghost.affinity_model import (
    AffinityEdgeSpec,
    AffinityNodeSpec,
    _bounded_refs,
    _clean_key,
    _clean_label,
    _clean_scope,
    _field,
    _list,
    _node_id,
    _unit_float,
)
from codey.ghost.schema import clip_signal_text
from codey.runtime.core import cancellation
from codey.storage.local_store import project_key, session_key

_HEBBIAN_KIND_MAP = {
    "style_preference": "user_preference",
    "correction": "correction",
    "action_tendency": "action_tendency",
    "research_interest": "research_concept",
}
_WORK_STATUS_REWARD = {
    "queued": 0.45,
    "running": 0.5,
    "done": 0.9,
    "blocked": 0.35,
}
_PROVIDER_ERROR_KINDS = frozenset(
    {
        "timeout",
        "parse_error",
        "tool_protocol_error",
        "transient",
        "rate_limited",
        "control_missing",
        "submission_uncertain",
        "response_missing",
        "readiness_stale",
        "authentication_required",
        "challenge_required",
        "transient_send_failed",
    }
)


def collect_source_specs(
    *,
    hebbian_store: Any,
    work_queue_store: Any,
    research_interest_candidates: Iterable[Any],
    run_projection: Any,
    terminal_event: Mapping[str, object] | None,
    session_id: str,
    project: str,
) -> tuple[list[AffinityNodeSpec], list[AffinityEdgeSpec]]:
    """Collect node/edge specs from every source with explicit inputs."""
    node_specs: list[AffinityNodeSpec] = []
    edge_specs: list[AffinityEdgeSpec] = []
    node_specs.extend(_node_specs_from_hebbian(hebbian_store))
    work_nodes, work_edges = _specs_from_work_queue(work_queue_store, session_id=session_id, project=project)
    node_specs.extend(work_nodes)
    edge_specs.extend(work_edges)
    research_nodes, research_edges = _specs_from_research_candidates(
        research_interest_candidates,
        session_id=session_id,
        project=project,
    )
    node_specs.extend(research_nodes)
    edge_specs.extend(research_edges)
    provider_nodes, provider_edges = _specs_from_provider_outcome(
        run_projection=run_projection,
        terminal_event=terminal_event,
        session_id=session_id,
        project=project,
    )
    node_specs.extend(provider_nodes)
    edge_specs.extend(provider_edges)
    return node_specs, edge_specs


def _node_specs_from_hebbian(hebbian_store: Any) -> list[AffinityNodeSpec]:
    if hebbian_store is None:
        return []
    try:
        rows = hebbian_store.list_nodes(status="active")
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return []
    specs: list[AffinityNodeSpec] = []
    for node in rows:
        if str(getattr(node, "status", "")) != "active" or getattr(node, "superseded_by", ""):
            continue
        affinity_kind = _HEBBIAN_KIND_MAP.get(str(getattr(node, "kind", "") or ""))
        if not affinity_kind:
            continue
        scope, scope_ref = _scope_from_source(node)
        conflict_key = _clean_key(getattr(node, "conflict_key", ""), 120)
        value_key = _clean_key(getattr(node, "value_key", ""), 120)
        if not conflict_key or not value_key:
            continue
        key = _clean_key(f"{getattr(node, 'kind', '')}:{conflict_key}:{value_key}", 180)
        label = _clean_label(f"{getattr(node, 'kind', '')}:{conflict_key}={value_key}", 180)
        source_refs = _bounded_refs(
            (
                f"hebbian_node:{clip_signal_text(getattr(node, 'id', ''), 120)}",
                *(f"hebbian_evidence:{ref}" for ref in _list(getattr(node, "evidence_refs", ()))),
            )
        )
        if not key or not label or not source_refs:
            continue
        evidence_refs = _bounded_refs(tuple(f"hebbian:{ref}" for ref in _list(getattr(node, "evidence_refs", ()))))
        specs.append(
            AffinityNodeSpec(
                kind=affinity_kind,
                key=key,
                label=label,
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(getattr(node, "confidence", 0.0)),
                reward=max(0.2, _unit_float(getattr(node, "weight", 0.0))),
                source_refs=source_refs,
                evidence_refs=evidence_refs,
                metadata={
                    "source": "hebbian",
                    "hebbian_node_id": clip_signal_text(getattr(node, "id", ""), 120),
                    "hebbian_kind": clip_signal_text(getattr(node, "kind", ""), 80),
                    "conflict_key": conflict_key,
                    "value_key": value_key,
                },
            )
        )
    return specs


def _specs_from_work_queue(
    work_queue_store: Any,
    *,
    session_id: str,
    project: str,
) -> tuple[list[AffinityNodeSpec], list[AffinityEdgeSpec]]:
    if work_queue_store is None:
        return [], []
    try:
        rows = work_queue_store.list_items()
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return [], []
    node_specs: list[AffinityNodeSpec] = []
    edge_specs: list[AffinityEdgeSpec] = []
    for item in rows:
        status = clip_signal_text(_field(item, "status"), 40)
        reward = _WORK_STATUS_REWARD.get(status)
        if reward is None:
            continue
        scope, scope_ref = _scope_from_source(item, fallback_session_id=session_id, fallback_project=project)
        item_id = clip_signal_text(_field(item, "id"), 120)
        task_kind = _clean_key(_field(item, "kind"), 80)
        if not item_id or not task_kind:
            continue
        item_ref = _bounded_refs((f"work_item:{item_id}:{status}:{clip_signal_text(_field(item, 'updated_at'), 80)}",))
        task_node = AffinityNodeSpec(
            kind="task_type",
            key=task_kind,
            label=f"task_type:{task_kind}",
            scope=scope,
            scope_ref=scope_ref,
            confidence=_unit_float(_field(item, "confidence")),
            reward=reward,
            source_refs=item_ref,
            metadata={"source": "work_queue", "work_status": status},
        )
        node_specs.append(task_node)
        task_id = _node_id(task_node.kind, task_node.scope, task_node.scope_ref, task_node.key)
        if scope == "project" and scope_ref:
            project_key_value = scope_ref
            project_node = AffinityNodeSpec(
                kind="project",
                key=project_key_value,
                label=f"project:{project_key_value}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=reward,
                source_refs=item_ref,
                metadata={"source": "work_queue"},
            )
            node_specs.append(project_node)
            project_id = _node_id(project_node.kind, project_node.scope, project_node.scope_ref, project_node.key)
            edge_specs.append(
                AffinityEdgeSpec(
                    source=project_id,
                    target=task_id,
                    relation="used_in_task",
                    scope=scope,
                    scope_ref=scope_ref,
                    confidence=_unit_float(_field(item, "confidence")),
                    reward=reward,
                    source_refs=item_ref,
                    proof_refs=_bounded_refs(_field(item, "proof_refs")) if status == "done" else (),
                )
            )
        relation = "works_well_for" if status == "done" else "struggles_with" if status == "blocked" else ""
        if relation and scope == "project" and scope_ref:
            provider_or_project = _node_id("project", scope, scope_ref, scope_ref)
            edge_specs.append(
                AffinityEdgeSpec(
                    source=task_id,
                    target=provider_or_project,
                    relation=relation,
                    scope=scope,
                    scope_ref=scope_ref,
                    confidence=_unit_float(_field(item, "confidence")),
                    reward=reward,
                    source_refs=item_ref,
                    proof_refs=_bounded_refs(_field(item, "proof_refs")) if status == "done" else item_ref,
                )
            )
        for concept in _concepts_from_work_item(item):
            concept_node = AffinityNodeSpec(
                kind="research_concept",
                key=concept,
                label=f"concept:{concept}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=min(0.8, reward),
                source_refs=item_ref,
                metadata={"source": "work_queue", "not_evidence": True},
            )
            node_specs.append(concept_node)
            concept_id = _node_id(concept_node.kind, concept_node.scope, concept_node.scope_ref, concept_node.key)
            edge_specs.append(
                AffinityEdgeSpec(
                    source=task_id,
                    target=concept_id,
                    relation="mentions_concept",
                    scope=scope,
                    scope_ref=scope_ref,
                    confidence=_unit_float(_field(item, "confidence")),
                    reward=min(0.8, reward),
                    source_refs=item_ref,
                    proof_refs=_bounded_refs(_field(item, "proof_refs")) if status == "done" else (),
                )
            )
    return node_specs, edge_specs


def _specs_from_research_candidates(
    candidates: Iterable[Any],
    *,
    session_id: str,
    project: str,
) -> tuple[list[AffinityNodeSpec], list[AffinityEdgeSpec]]:
    node_specs: list[AffinityNodeSpec] = []
    edge_specs: list[AffinityEdgeSpec] = []
    for candidate in list(candidates or []):
        candidate_id = clip_signal_text(_field(candidate, "id"), 120)
        if not candidate_id:
            continue
        scope, scope_ref = _scope_from_source(candidate, fallback_session_id=session_id, fallback_project=project)
        confidence = _unit_float(_field(candidate, "confidence"))
        reward = max(0.35, _unit_float(_field(candidate, "priority")))
        refs = _bounded_refs(
            (
                f"research_interest:{candidate_id}",
                *_list(_field(candidate, "source_refs")),
            )
        )
        concepts = _concepts_from_candidate(candidate)
        concept_ids: list[str] = []
        for concept in concepts:
            node_spec = AffinityNodeSpec(
                kind="research_concept",
                key=concept,
                label=f"concept:{concept}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=confidence,
                reward=reward,
                source_refs=refs,
                evidence_refs=(),
                metadata={
                    "source": clip_signal_text(_field(candidate, "source"), 80),
                    "not_evidence": True,
                },
            )
            node_specs.append(node_spec)
            concept_ids.append(_node_id(node_spec.kind, node_spec.scope, node_spec.scope_ref, node_spec.key))
        if len(concept_ids) >= 2:
            for index, source in enumerate(concept_ids):
                for target in concept_ids[index + 1 :]:
                    edge_specs.append(
                        AffinityEdgeSpec(
                            source=source,
                            target=target,
                            relation="associated_with",
                            scope=scope,
                            scope_ref=scope_ref,
                            confidence=confidence,
                            reward=reward,
                            source_refs=refs,
                            proof_refs=(),
                        )
                    )
    return node_specs, edge_specs


def _specs_from_provider_outcome(
    *,
    run_projection: Any,
    terminal_event: Mapping[str, object] | None,
    session_id: str,
    project: str,
) -> tuple[list[AffinityNodeSpec], list[AffinityEdgeSpec]]:
    if run_projection is None and not isinstance(terminal_event, Mapping):
        return [], []
    node_specs: list[AffinityNodeSpec] = []
    edge_specs: list[AffinityEdgeSpec] = []
    failures = []
    if run_projection is not None:
        failures.extend(list(getattr(run_projection, "provider_failures", ()) or ()))
    if isinstance(terminal_event, Mapping) and isinstance(terminal_event.get("provider_failure"), Mapping):
        failures.append(terminal_event.get("provider_failure"))
    scope = "project" if project else "session" if session_id else "user"
    scope_ref = _scope_ref(scope, project or session_id)
    task_mode = _clean_key(
        getattr(run_projection, "mode", "")
        or (terminal_event.get("mode") if isinstance(terminal_event, Mapping) else "")
        or "task",
        80,
    )
    run_ref = _bounded_refs(
        (f"run:{clip_signal_text(getattr(run_projection, 'run_id', '') or (terminal_event or {}).get('run_id'), 120)}",)
    )
    task_spec = AffinityNodeSpec(
        kind="task_type",
        key=task_mode,
        label=f"task_type:{task_mode}",
        scope=scope,
        scope_ref=scope_ref,
        confidence=0.6,
        reward=0.2,
        source_refs=run_ref,
    )
    if task_mode and run_ref:
        node_specs.append(task_spec)
    task_id = _node_id(task_spec.kind, task_spec.scope, task_spec.scope_ref, task_spec.key) if task_mode else ""
    for failure in failures:
        provider = _clean_key(
            _field(failure, "provider") or _field(failure, "model") or (terminal_event or {}).get("provider"), 80
        )
        error_kind = _clean_provider_error_kind(_field(failure, "kind"))
        action = _clean_key(_field(failure, "action"), 80)
        stage = _clean_key(_field(failure, "stage"), 80)
        if not provider or not error_kind:
            continue
        refs = _bounded_refs(
            (
                "provider_failure:"
                + hashlib.sha256(
                    "|".join(
                        (
                            clip_signal_text(
                                getattr(run_projection, "run_id", "") or (terminal_event or {}).get("run_id"), 120
                            ),
                            provider,
                            error_kind,
                            action,
                            stage,
                        )
                    ).encode("utf-8", errors="replace")
                ).hexdigest()[:24],
            )
        )
        if not refs:
            continue
        provider_spec = AffinityNodeSpec(
            kind="provider_behavior",
            key=f"{provider}:{error_kind}:{action}:{stage}",
            label=f"provider:{provider}:{error_kind}",
            scope=scope,
            scope_ref=scope_ref,
            confidence=0.75,
            reward=0.55,
            source_refs=refs,
            metadata={
                "source": "provider_failure",
                "provider": provider,
                "error_kind": error_kind,
                "action": action,
                "stage": stage,
            },
        )
        node_specs.append(provider_spec)
        if task_id:
            edge_specs.append(
                AffinityEdgeSpec(
                    source=_node_id(
                        provider_spec.kind, provider_spec.scope, provider_spec.scope_ref, provider_spec.key
                    ),
                    target=task_id,
                    relation="struggles_with",
                    scope=scope,
                    scope_ref=scope_ref,
                    confidence=0.75,
                    reward=0.45,
                    source_refs=refs,
                )
            )
    return node_specs, edge_specs


def _scope_from_source(
    value: Any,
    *,
    fallback_session_id: str = "",
    fallback_project: str = "",
) -> tuple[str, str]:
    scope = _clean_scope(_field(value, "scope"))
    raw_ref = clip_signal_text(_field(value, "scope_ref"), 240)
    if not scope:
        scope = "session" if fallback_session_id else "project" if fallback_project else "user"
        raw_ref = fallback_session_id or fallback_project
    if scope == "session":
        raw_ref = raw_ref or clip_signal_text(_field(value, "session_id"), 120) or fallback_session_id
    elif scope == "project":
        raw_ref = raw_ref or clip_signal_text(_field(value, "project"), 240) or fallback_project
    return scope, _scope_ref(scope, raw_ref)


def _scope_ref(scope: str, raw_ref: object) -> str:
    from codey.ghost.affinity_model import _clean_scope as _clean_scope_value

    clean_scope = _clean_scope_value(scope)
    text = clip_signal_text(raw_ref, 240)
    if clean_scope == "session":
        return text if _looks_like_hash_ref(text) else session_key(text) if text else ""
    if clean_scope == "project":
        if not text:
            return ""
        if _looks_like_hash_ref(text):
            return text
        try:
            return project_key(text)
        except (OSError, RuntimeError, ValueError):
            return hashlib.sha256(text.casefold().encode("utf-8", errors="replace")).hexdigest()[:24]
    return ""


def _looks_like_hash_ref(value: object) -> bool:
    text = str(value or "").strip()
    return len(text) == 24 and all(ch in "0123456789abcdef" for ch in text)


def _concepts_from_work_item(item: Any) -> tuple[str, ...]:
    metadata = _field(item, "metadata")
    if not isinstance(metadata, Mapping):
        return ()
    return _clean_concepts(
        (
            *_metadata_sequence(metadata.get("related_concepts")),
            *_metadata_sequence(metadata.get("shared_neighbors")),
        )
    )


def _concepts_from_candidate(candidate: Any) -> tuple[str, ...]:
    return _clean_concepts(
        (*_list(_field(candidate, "related_concepts")), *_list(_field(candidate, "shared_neighbors")))
    )


def _clean_concepts(values: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = _clean_key(value, 120).casefold()
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= 8:
            break
    return tuple(out)


def _clean_provider_error_kind(value: object) -> str:
    text = _clean_key(value, 80)
    return text if text in _PROVIDER_ERROR_KINDS else "transient" if text else ""


def _metadata_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    text = str(value or "").strip()
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            return tuple(parsed)
    return (text,)


__all__ = ["collect_source_specs"]
