"""Short labels for explicitly read knowledge; no executor or private recovery log."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from codey.knowledge.answer_refs import answer_references, answer_scope
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
from codey.research.ledger import EvidenceItem, OpenedSource, ResearchLedger, SourceQuality

if TYPE_CHECKING:
    from codey.research.tools import ResearchToolOutput


def read_version(store: Any, identity: str, revision: int | None = None) -> KnowledgeVersion | None:
    """Read a staged mutation at its exact prospective version, or the repository."""
    for _commit, mutations in getattr(store, "_staged_knowledge", {}).values():
        for mutation in mutations:
            if mutation.value.id == identity and revision in {None, mutation.expected_revision + 1}:
                return KnowledgeVersion(mutation.value, mutation.expected_revision + 1, "", _commit)
    return cast(KnowledgeVersion | None, store.repository.read(identity, revision))


def bind_version(ledger: ResearchLedger, version: KnowledgeVersion) -> str:
    row = {"id": version.value.id, "revision": version.revision}
    for label, previous in ledger.knowledge_refs.items():
        if row == previous:
            return label
    number = max((int(key[1:]) for key in (*ledger.finding_refs, *ledger.knowledge_refs)), default=0) + 1
    label = f"f{number}"
    ledger.knowledge_refs[label] = row
    return label


def restore_bindings(ledger: ResearchLedger, raw: object) -> None:
    if not isinstance(raw, dict) or len(raw) > 256:
        raise ValueError("invalid research reference bindings")
    pending = dict(ledger.knowledge_refs)
    for label, row in raw.items():
        if (not isinstance(label, str) or re.fullmatch(r"f[1-9][0-9]*", label) is None
                or not isinstance(row, dict) or set(row) != {"id", "revision"}):
            raise ValueError("invalid research reference binding")
        KnowledgeReference(row["id"], row["revision"], "uses")
        if label in ledger.finding_refs or label in pending and pending[label] != row:
            raise ValueError("conflicting research reference binding")
        if any(key != label and value == row for key, value in pending.items()):
            raise ValueError("duplicate research reference identity")
        pending[label] = dict(row)
    ledger.knowledge_refs = pending


def resolve_version(ledger: ResearchLedger, store: Any, label: str) -> KnowledgeVersion:
    row = ledger.knowledge_refs.get(label)
    if row:
        version = read_version(store, str(row["id"]), int(row["revision"]))
    else:
        note_id = ledger.finding_refs.get(label)
        versions: list[KnowledgeVersion | None] = [read_version(store, str(ref["target"]), int(ref["revision"]))
                    for ref in store.note_references(note_id)] if note_id else []
        claims = [v for v in versions if v is not None and v.value.kind == "claim"]
        version = claims[0] if len(claims) == 1 else None
    if version is None or version.value.kind not in {"claim", "inference", "observation"}:
        raise ValueError(f"Unknown or non-premise reference [{label}]; read a claim or inference first")
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
        label = bind_version(tools.ledger, version)
        return ResearchToolOutput(canonical_json({"finding_ref": label, "statement": value.content,
            "kind": "inference", "status": "candidate", "interpretation_review": "unreviewed",
            "limitations": limitations, "premise_refs": labels,
            "next": "This is a candidate inference, not direct evidence. Cite it explicitly as a hypothesis."}), ok=True)
    except (ValueError, TypeError, KeyError) as exc:
        return ResearchToolOutput(f"ERROR: {exc}", ok=False)


def evidence_view(ledger: ResearchLedger, store: Any, labels: list[str], *, project: str, session_id: str
                  ) -> tuple[ResearchLedger, dict[str, KnowledgeVersion], dict[str, list[str]]]:
    """A validation view of archived evidence, never a record of fresh acquisition.

    The original execution ledger is untouched. Inferences keep depends_on edges;
    their premises' excerpts never become supporting excerpts for the inference.
    """
    view = ledger.clone()
    versions = {label: resolve_version(ledger, store, label) for label in labels}
    urls: dict[str, list[str]] = {}
    access = KnowledgeAccess(project, session_id, store.owner_id)

    def visit(version: KnowledgeVersion, path: frozenset[tuple[str, int]]) -> list[str]:
        key = version.value.id, version.revision
        if key in path or len(path) >= 12:
            raise ValueError("cyclic or overly deep knowledge premises")
        if not access.allows(version) or version.value.kind == "answer":
            raise ValueError("inaccessible premise or old answer used as evidence")
        found = []
        for ref in version.value.references:
            if ref.relation not in {"supported_by", "limited_by", "refuted_by", "depends_on"}:
                continue
            target = read_version(store, ref.target, ref.revision)
            if target is None or not access.allows(target):
                raise ValueError("missing or inaccessible evidence version")
            if ref.relation == "depends_on":
                found.extend(visit(target, path | {key}))
                continue
            if target.value.kind != "evidence":
                raise ValueError("claim evidence reference does not target evidence")
            for source_ref in target.value.references:
                if source_ref.relation != "from_source":
                    continue
                source = read_version(store, source_ref.target, source_ref.revision)
                if source is None or source.value.kind != "source" or not access.allows(source):
                    raise ValueError("missing or inaccessible original source version")
                url = str(source.value.provenance.get("url") or "")
                if not url.startswith(("https://", "http://")) or target.value.content not in source.value.content:
                    raise ValueError("evidence fragment is not in the pinned source version")
                # View-only historical source; never claim that it was fetched now.
                provenance = source.value.provenance
                view.merge_opened_source(OpenedSource(url, url, source.value.title,
                    str(provenance.get("retrieved_at", source.recorded_at)),
                    str(provenance.get("content_hash", "")), SourceQuality(**provenance.get("quality", {})),
                    content_kind=str(provenance.get("content_kind", "html")),
                    truncated=bool(provenance.get("truncated", False))), source.value.content, {})
                stance = {"supported_by": "supports", "limited_by": "limits", "refuted_by": "contradicts"}[ref.relation]
                item = EvidenceItem(version.value.content, url, target.value.content, stance,
                                    note_id=version.value.id)
                if item not in view.evidence_items:
                    view.evidence_items.append(item)
                found.append(url)
        return list(dict.fromkeys(found))

    for label, version in versions.items():
        # Recheck scope at final adoption, including exact historical revisions.
        answer_references([{"id": version.value.id, "revision": version.revision}], store,
                          project=project, session_id=session_id)
        urls[label] = visit(version, frozenset())
        if not urls[label]:
            raise ValueError(f"[{label}] has no source-backed premise; preserve it as an open hypothesis, not report evidence")
    return view, versions, urls
