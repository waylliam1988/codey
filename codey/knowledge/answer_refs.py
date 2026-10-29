"""Validate explicitly cited saved knowledge versions, without semantic scoring."""

from __future__ import annotations

from typing import Any

from codey.knowledge.model import KnowledgeReference
from codey.knowledge.recall import KnowledgeAccess


def answer_references(raw: Any, store: Any, *, project: str = "", session_id: str = "") -> tuple[KnowledgeReference, ...]:
    if raw is None or raw == ():
        return ()
    if not isinstance(raw, list) or len(raw) > 32:
        raise ValueError("knowledge_refs must be a bounded list of {id, revision}")
    refs = []
    access = KnowledgeAccess(project, session_id, getattr(store, "owner_id", ""))
    for row in raw:
        if not isinstance(row, dict) or set(row) != {"id", "revision"}:
            raise ValueError("knowledge_refs require id and revision")
        reference = KnowledgeReference(row["id"], row["revision"], "uses")
        version = store.repository.read(reference.target, reference.revision) if store is not None else None
        if version is None or not access.allows(version):
            raise ValueError("cited knowledge version is missing or inaccessible")
        if version.value.kind == "answer":
            raise ValueError("cite the original premises rather than an old answer")
        refs.append(reference)
    return tuple(dict.fromkeys(refs))


def answer_scope(references: list[KnowledgeReference], store: Any, *, project: str, session_id: str) -> tuple[str, str]:
    """A report inherits restricted premises instead of publishing them globally."""
    restricted = {(version.value.scope, version.value.scope_id) for ref in references
                  if (version := store.repository.read(ref.target, ref.revision)) is not None
                  and version.value.scope != "global"}
    if len(restricted) > 1:
        raise ValueError("report combines incompatible restricted scopes")
    return next(iter(restricted)) if restricted else (
        ("project", project) if project else (("session", session_id) if session_id else ("global", "")))
