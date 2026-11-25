"""Validate explicitly cited saved knowledge versions, without semantic scoring."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Protocol

from codey.knowledge.model import KnowledgeReference, KnowledgeVersion
from codey.knowledge.recall import KnowledgeAccess
from codey.types.json_value import decode_json_value, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.knowledge.repository import KnowledgeRepository


class ReferenceStore(Protocol):
    @property
    def owner_id(self) -> str: ...

    @property
    def repository(self) -> KnowledgeRepository: ...


class KnowledgeReferenceError(ValueError):
    """A rejected exact identity, retained for completion feedback."""

    def __init__(self, message: str, reference: KnowledgeReference) -> None:
        super().__init__(message)
        self.reference: KnowledgeReference = reference


def answer_references(raw: object, store: ReferenceStore | None, *,
                      project: str = "", session_id: str = "") -> tuple[KnowledgeReference, ...]:
    if raw is None or raw == ():
        return ()
    rows = decode_json_value(raw)
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError("knowledge_refs must be a bounded list of {id, revision}")
    refs: list[KnowledgeReference] = []
    access = KnowledgeAccess(project, session_id, store.owner_id if store is not None else "")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "revision"}:
            raise ValueError("knowledge_refs require id and revision")
        reference = KnowledgeReference(require_json_string(row["id"], "knowledge id"),
                                       require_json_integer(row["revision"], "knowledge revision"), "uses")
        version = store.repository.read(reference.target, reference.revision) if store is not None else None
        if version is None or not access.allows(version):
            raise KnowledgeReferenceError("cited knowledge version is missing or inaccessible", reference)
        if version.value.kind == "answer":
            raise KnowledgeReferenceError("cite the original premises rather than an old answer", reference)
        refs.append(reference)
    return tuple({ref: None for ref in refs})


def answer_scope(references: list[KnowledgeReference], store: ReferenceStore, *,
                 project: str, session_id: str) -> tuple[str, str]:
    """A report inherits restricted premises instead of publishing them globally."""
    def versions() -> Iterator[KnowledgeVersion]:
        for reference in references:
            version = store.repository.read(reference.target, reference.revision)
            if version is None:
                raise KnowledgeReferenceError("cited knowledge version is missing or inaccessible", reference)
            yield version

    return scope_for_versions(versions(), project=project, session_id=session_id)


def scope_for_versions(versions: Iterable[KnowledgeVersion], *, project: str, session_id: str) -> tuple[str, str]:
    restricted = {(version.value.scope, version.value.scope_id) for version in versions if version.value.scope != "global"}
    if len(restricted) > 1:
        raise ValueError("report combines incompatible restricted scopes")
    return next(iter(restricted)) if restricted else (
        ("project", project) if project else (("session", session_id) if session_id else ("global", "")))
