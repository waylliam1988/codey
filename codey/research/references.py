"""Short labels for explicitly read knowledge; no executor or private recovery log."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from codey.knowledge.answer_refs import answer_scope
from codey.knowledge.archive import stable_identity
from codey.knowledge.model import (
    KnowledgeMutation,
    KnowledgeObject,
    KnowledgeReference,
    KnowledgeVersion,
    canonical_json,
)
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.recall import KnowledgeAccess
from codey.research.ledger import ResearchLedger
from codey.research.reference import ResearchReference

if TYPE_CHECKING:
    from codey.research.tools import ResearchToolOutput


def read_version(store: Any, identity: str, revision: int | None = None) -> KnowledgeVersion | None:
    """Read a staged mutation at its exact prospective version, or the repository."""
    for _commit, mutations in getattr(store, "_staged_knowledge", {}).values():
        for mutation in mutations:
            if mutation.value.id == identity and revision in {None, mutation.expected_revision + 1}:
                return KnowledgeVersion(mutation.value, mutation.expected_revision + 1, "", _commit)
    return cast(KnowledgeVersion | None, store.repository.read(identity, revision))


def bind_version(ledger: ResearchLedger, version: KnowledgeVersion, *, acquisition: str = "prior_knowledge",
                 note_id: str = "", passage_ref: str = "") -> str:
    for label, previous in ledger.references.items():
        if (version.value.id, version.revision) == (previous.object_id, previous.revision):
            return label
    label = f"f{max((int(key[1:]) for key in ledger.references), default=0) + 1}"
    ledger.references[label] = ResearchReference(version.value.id, version.revision, version.value.kind,
                                                acquisition, note_id, passage_ref)
    return label


def restore_bindings(ledger: ResearchLedger, raw: object) -> None:
    if not isinstance(raw, dict) or len(raw) > 256:
        raise ValueError("invalid research reference bindings")
    pending = dict(ledger.references)
    for label, row in raw.items():
        if (not isinstance(label, str) or re.fullmatch(r"f[1-9][0-9]*", label) is None
                or not isinstance(row, dict) or set(row) != {
                    "object_id", "revision", "kind", "acquisition", "note_id", "passage_ref"}):
            raise ValueError("invalid research reference binding")
        value = ResearchReference(**row)
        if label in pending and pending[label] != value:
            raise ValueError("conflicting research reference binding")
        if any(key != label and (old.object_id, old.revision) == (value.object_id, value.revision)
               for key, old in pending.items()):
            raise ValueError("duplicate research reference identity")
        pending[label] = value
    ledger.references = pending


def resolve_version(ledger: ResearchLedger, store: Any, label: str) -> KnowledgeVersion:
    binding = ledger.references.get(label)
    version = read_version(store, binding.object_id, binding.revision) if binding else None
    if version is None or binding is None or version.value.kind != binding.kind:
        raise ValueError(f"Unknown or inconsistent premise reference [{label}]; read a claim or inference first")
    return version


def save_inference(tools: Any, args: dict[str, Any]) -> ResearchToolOutput:
    from codey.research.tools import ResearchToolOutput

    try:
        statement, labels, limitations = args.get("statement"), args.get("premise_refs"), args.get("limitations")
        if (not isinstance(statement, str) or not 12 <= len(statement.strip()) <= 4000
                or not isinstance(limitations, str) or not 1 <= len(limitations.strip()) <= 4000
                or not isinstance(labels, list) or not 1 <= len(labels) <= 16
                or any(not isinstance(label, str) for label in labels) or len(set(labels)) != len(labels)):
            raise ValueError("save_inference needs statement (12..4000 characters), premise_refs (1..16 distinct fN), limitations")
        if tools.store is None:
            raise ValueError("knowledge store is unavailable")
        versions = [resolve_version(tools.ledger, tools.store, label) for label in labels]
        access = KnowledgeAccess(tools.project, tools.session_id, tools.store.owner_id)
        if any(not access.allows(version) for version in versions):
            raise ValueError("knowledge scope mismatch")
        refs = [KnowledgeReference(v.value.id, v.revision, "depends_on") for v in versions]
        scope, scope_id = answer_scope(refs, tools.store, project=tools.project, session_id=tools.session_id)
        identity = stable_identity("inference", tools.session_id, tools.project, statement,
                                   canonical_json([{"id": r.target, "revision": r.revision} for r in refs]), limitations)
        value = KnowledgeObject(identity, "inference", statement.strip(), "model_inference",
            title=statement[:160], scope=scope, scope_id=scope_id, status="candidate", references=tuple(refs),
            provenance={"limitations": limitations.strip(), "session_id": tools.session_id,
                        "basis": "candidate_inference_not_direct_source_evidence"})
        previous = read_version(tools.store, identity)
        if previous is not None and previous.value != value:
            raise ValueError("inference identity conflict")
        if previous is None:
            note = KnowledgeNote.create(type="hypothesis", title=value.title, body=value.content,
                session_id=tools.session_id, project=tools.project, id=identity)
            tools.store.publish_note(note, stable_identity("inference-commit", identity),
                                     [KnowledgeMutation(value)], changes=tools.changes)
            if identity not in tools.created_ids:
                tools.created_ids.append(identity)
        version = read_version(tools.store, identity)
        assert version is not None
        label = bind_version(tools.ledger, version, acquisition="current_run", note_id=identity)
        return ResearchToolOutput(canonical_json({"finding_ref": label, "statement": value.content,
            "kind": "inference", "status": "candidate", "interpretation_review": "unreviewed",
            "limitations": limitations, "premise_refs": labels,
            "next": "This is a candidate inference, not direct evidence. Cite it explicitly as a hypothesis."}), ok=True)
    except (ValueError, TypeError, KeyError) as exc:
        return ResearchToolOutput(f"ERROR: {exc}", ok=False)
