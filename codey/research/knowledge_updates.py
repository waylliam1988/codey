"""Compile explicitly authored new knowledge into the original answer transaction."""

from __future__ import annotations

import re
from typing import Any

from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
from codey.research.knowledge_schema import condition_map_schema, object_schema

MAX_UPDATES = 16
_KEY_PATTERN = r"[A-Za-z][A-Za-z0-9_-]{0,63}"
_REF_PATTERN = rf"(?:{_KEY_PATTERN}|\[{_KEY_PATTERN}\])"
_EVIDENCE_LABEL = re.compile(r"e[1-9][0-9]*")
_RELATIONS = {"claim": {"supported_by", "limited_by", "refuted_by"},
              "inference": {"depends_on"}, "question": {"motivated_by"}}
_DEFAULT_RELATION = {"claim": "supported_by", "inference": "depends_on", "question": "motivated_by"}


def updates_schema() -> dict[str, Any]:
    return {"type": "array", "maxItems": MAX_UPDATES, "items": object_schema({
        "key": {"type": "string", "pattern": f"^{_KEY_PATTERN}$",
                "description": "Unique batch-local key, e.g. k1 or writer_limit. Evidence labels eN are reserved."},
        "kind": {"type": "string", "enum": list(_RELATIONS)},
        "content": {"type": "string", "minLength": 12, "maxLength": 4000},
        "title": {"type": "string", "maxLength": 200},
        "aliases": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        "conditions": condition_map_schema(),
        "references": _refs_schema("Visible eN evidence for claims; all eN/batch-key premises for inferences. "
                                   "Use a string list such as [\"e1\"] or [\"k1\",\"e1\"]."),
        "limited_by": _refs_schema("Optional limiting eN evidence, for claims only."),
        "refuted_by": _refs_schema("Optional contrary eN evidence, for claims only."),
    }, "key", "kind", "content", "references")}


def _refs_schema(description: str) -> dict[str, Any]:
    return {"type": "array", "maxItems": 16, "description": description,
            "items": {"type": "string", "pattern": f"^{_REF_PATTERN}$"}}


def _normalize_references(raw: Any, kind: str) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > 16:
        raise ValueError("knowledge update references must be a list of at most 16 evidence or batch keys")
    # The public contract uses strings. Exact relation objects remain readable
    # in frozen internal v2 submissions; they are never silently reinterpreted.
    refs = [{"ref": ref, "relation": _DEFAULT_RELATION[kind]} if isinstance(ref, str) else ref for ref in raw]
    if any(not isinstance(ref, dict) or set(ref) != {"ref", "relation"}
           or not isinstance(ref["ref"], str) or re.fullmatch(_REF_PATTERN, ref["ref"]) is None
           or not isinstance(ref["relation"], str) or ref["relation"] not in _RELATIONS[kind] for ref in refs):
        raise ValueError(f"knowledge update {kind} references require visible eN or declared batch keys; "
                         "use string lists, e.g. references=[\"e1\"] or [\"k1\",\"e1\"]")
    return [{"ref": ref["ref"][1:-1] if ref["ref"].startswith("[") else ref["ref"],
             "relation": ref["relation"]} for ref in refs]


def _normalize_update(row: Any) -> dict[str, Any]:
    required = {"key", "kind", "content", "references"}
    if (not isinstance(row, dict) or not required <= row.keys()
            or row.keys() - required - {"title", "aliases", "conditions", "limited_by", "refuted_by"}):
        raise ValueError("knowledge_updates entries need key, kind, content and references; no database identities or scope")
    key, kind, content = row["key"], row["kind"], row["content"]
    if (not isinstance(key, str) or re.fullmatch(_KEY_PATTERN, key) is None or _EVIDENCE_LABEL.fullmatch(key)
            or not isinstance(kind, str) or kind not in _RELATIONS
            or not isinstance(content, str) or not 12 <= len(content.strip()) <= 4000):
        raise ValueError("knowledge_updates need a unique batch key (e.g. k1), claim/inference/question kind, "
                         "and content (12..4000 characters); eN keys are reserved for evidence")
    title, aliases, conditions = row.get("title", ""), row.get("aliases", []), row.get("conditions", {})
    if (not isinstance(title, str) or len(title) > 200 or not isinstance(aliases, list) or len(aliases) > 8
            or any(not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 200 for alias in aliases)):
        raise ValueError("knowledge_updates title/aliases exceed their bounds")
    KnowledgeObject(key, kind, content.strip(), "model_extraction" if kind == "claim" else "model_inference",
                    title=title, aliases=tuple(aliases), conditions=conditions)
    refs = _normalize_references(row["references"], kind)
    for relation in ("limited_by", "refuted_by"):
        extra = row.get(relation, [])
        if not isinstance(extra, list) or any(not isinstance(ref, str) for ref in extra) or extra and kind != "claim":
            raise ValueError(f"{relation} must be an eN string list on a claim")
        refs.extend(_normalize_references([{"ref": ref, "relation": relation} for ref in extra], kind))
    if len(refs) > 16:
        raise ValueError("knowledge update references exceed the total limit of 16")
    if len({(ref["ref"], ref["relation"]) for ref in refs}) != len(refs):
        raise ValueError("duplicate knowledge update reference")
    if kind == "claim" and (not any(ref["relation"] == "supported_by" for ref in refs)
                            or any(_EVIDENCE_LABEL.fullmatch(ref["ref"]) is None for ref in refs)):
        raise ValueError("new claims require supported_by original eN evidence")
    if kind == "inference" and not refs:
        raise ValueError("new inferences require pinned premises")
    return {"key": key, "kind": kind, "content": content.strip(), "title": title,
            "aliases": aliases, "conditions": conditions, "references": refs}


def normalize_updates(raw: Any, labels: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_UPDATES:
        raise ValueError(f"knowledge_updates must be a list of at most {MAX_UPDATES} explicit entries")
    rows = [_normalize_update(row) for row in raw]
    by_key = {row["key"]: row for row in rows}
    if len(by_key) != len(rows):
        raise ValueError("duplicate knowledge update key")
    for row in rows:
        for ref in row["references"]:
            target = ref["ref"]
            if _EVIDENCE_LABEL.fullmatch(target):
                if target not in labels:
                    raise ValueError(f"knowledge_updates references unknown or unshown evidence [{target}]")
            elif target not in by_key:
                raise ValueError(f"knowledge_updates references unknown batch key [{target}]")
            elif row["kind"] == "inference" and by_key[target]["kind"] not in {"claim", "inference"}:
                raise ValueError("inference premises must be claims, inferences or original evidence")
    _check_cycles(by_key)
    return rows


def _check_cycles(rows: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("cyclic knowledge update premises")
        if key in visited:
            return
        visiting.add(key)
        for ref in rows[key]["references"]:
            if ref["ref"] in rows:
                visit(ref["ref"])
        visiting.remove(key)
        visited.add(key)

    for key in rows:
        visit(key)


def evidence_labels(updates: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(ref["ref"] for row in updates for ref in row["references"]
                             if _EVIDENCE_LABEL.fullmatch(ref["ref"])))


def compile_updates(updates: list[dict[str, Any]], evidence: Any, *, scope: str, owner: str,
                    namespace: str, submission_digest: str) -> list[dict[str, Any]]:
    """No writes, model calls or current-head resolution; all objects remain candidates."""
    bindings = {row["label"]: (row["evidence_id"], row["evidence_revision"]) for row in evidence}
    updates = normalize_updates(updates, set(bindings))
    targets = {row["key"]: (stable_identity(row["kind"], scope, owner, namespace, row["key"]), 1) for row in updates}
    targets.update(bindings)
    mutations = []
    for row in updates:
        value = KnowledgeObject(targets[row["key"]][0], row["kind"], row["content"],
            "model_extraction" if row["kind"] == "claim" else "model_inference", title=row["title"],
            scope=scope, scope_id=owner, aliases=tuple(row["aliases"]), conditions=row["conditions"],
            provenance={"basis": "explicit_research_update", "submission_digest": submission_digest, "key": row["key"]},
            references=tuple(KnowledgeReference(*targets[ref["ref"]], ref["relation"]) for ref in row["references"]))
        mutation = KnowledgeMutation(value)
        mutations.append({"value": mutation.value.to_dict(), "expected_revision": mutation.expected_revision})
    return mutations
