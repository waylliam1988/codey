from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.knowledge.changes import KnowledgeChanges
from codey.research.pipeline import ResearchContinuationError
from codey.research.staging import StagedKnowledgeStore
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)

if TYPE_CHECKING:
    from codey.research.tools import ResearchTools


def restore_note_proposals(tools: ResearchTools, canonical: Mapping[str, object]) -> None:
    """Rebuild note metadata and in-memory proposals from the original receipt.

    Restoring a tool result never writes the vault. Only the existing staged
    commit applies proposals after candidate selection.
    """
    from codey.knowledge.note import KnowledgeNote
    from codey.research.staging import StagedKnowledgeStore

    links = canonical.get("knowledge_links_created", 0)
    if type(links) is not int or links < 0:
        raise ResearchContinuationError("invalid knowledge link count")
    if links:
        tools.links_created += links
    raw_notes = canonical.get("knowledge_notes")
    if raw_notes is None:
        return
    payload = decode_json_object(raw_notes)
    if set(payload) != {"created", "updated", "proposals"}:
        raise ResearchContinuationError("invalid knowledge proposal receipt")
    created = require_json_strings(payload["created"], "created")
    updated = require_json_strings(payload["updated"], "updated")
    for values, target in ((created, tools.created_ids), (updated, tools.updated_ids)):
        if any(not item for item in values):
            raise ResearchContinuationError("invalid knowledge note identity")
        target.extend(item for item in values if item not in target)
    proposals = payload["proposals"]
    if not isinstance(proposals, list):
        raise ResearchContinuationError("invalid staged note proposals")
    bases = decode_json_object(canonical.get("knowledge_bases", {}))
    affected = {*created, *updated}
    if set(bases) - affected or any(value is not None and not isinstance(value, str) for value in bases.values()):
        raise ResearchContinuationError("invalid staged document base")
    batches = decode_json_object(canonical.get("knowledge_batches", {}))
    if not proposals and not batches:
        return
    store = tools.store
    if not isinstance(store, StagedKnowledgeStore):
        raise ResearchContinuationError("knowledge proposals require their original staging store")
    for raw in proposals:
        note = KnowledgeNote.from_payload(raw)
        if note.id not in affected:
            raise ResearchContinuationError("staged proposal changed its original identity or content")
        existing = store._staged_notes.get(note.id)
        if existing is not None and existing != note and note.id not in updated:
            raise ResearchContinuationError("conflicting staged note proposal")
        committed = store._parent.read_note(note.id)
        if (committed is not None and note.id not in bases and note.id not in updated
                and replace(committed, updated=note.updated).to_markdown() != note.to_markdown()):
            raise ResearchContinuationError("committed note differs from its original proposal")
        if note.id in bases:
            base = bases[note.id]
            if base is not None and not isinstance(base, str):
                raise ResearchContinuationError("invalid staged document base")
            if note.id in store._staged_bases and store._staged_bases[note.id] != base:
                raise ResearchContinuationError("staged document changed its original base")
            store._staged_bases[note.id] = base
        store.write_note(note, changes=tools.changes if isinstance(tools.changes, KnowledgeChanges) else None)
    _restore_knowledge_batches(store, batches, affected)


def project_urls(tools: ResearchTools) -> None:
    tools.sources_read.update(tools.ledger.final_url_set())
    tools.sources_read.update(source.requested_url for source in tools.ledger.opened_sources)
    tools.search_result_urls.update(hit.url for search in tools.ledger.searches for hit in search.results)


def _restore_knowledge_batches(store: StagedKnowledgeStore, batches: JsonObject, affected: set[str]) -> None:
    from codey.knowledge.model import KnowledgeMutation, KnowledgeObject

    for identity, raw_batch in batches.items():
        batch = decode_json_object(raw_batch)
        if identity not in affected or set(batch) != {"commit_id", "mutations"}:
            raise ResearchContinuationError("invalid knowledge batch proposal")
        rows = batch["mutations"]
        if not isinstance(rows, list):
            raise ResearchContinuationError("invalid knowledge batch mutations")
        mutations: list[KnowledgeMutation] = []
        for raw in rows:
            row = decode_json_object(raw)
            rev = row["expected_revision"]
            expected = require_json_integer(rev, "expected revision")
            mutations.append(KnowledgeMutation(KnowledgeObject.from_dict(decode_json_object(row["value"])), expected))
        store._staged_knowledge[identity] = (require_json_string(batch["commit_id"], "commit id"), mutations)
