"""One-finding input over the existing note transaction and execution effect."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from codey.knowledge.model import canonical_json
from codey.research.ledger import EvidenceItem, ResearchLedger

if TYPE_CHECKING:
    from codey.research.tools import ResearchToolOutput, ResearchTools


def save_finding(tools: ResearchTools, args: dict[str, Any]) -> ResearchToolOutput:
    from codey.research.tools import ResearchToolOutput

    try:
        if set(args) - {"claim", "source_ref", "passage_ref", "additional_passage_refs", "conditions", "comparison", "question_ref"}:
            raise ValueError("unexpected save_finding argument")
        from codey.research.questions import question_ref

        question = question_ref(args, tools.ledger)
        claim = args.get("claim")
        if not isinstance(claim, str) or not 12 <= len(claim.strip()) <= 4000:
            raise ValueError("claim requires one complete scoped finding (12..4000 characters)")
        claim = claim.strip()
        ref = args.get("passage_ref")
        passage = tools.ledger.read_passages.get(ref) if isinstance(ref, str) else None
        if passage is None:
            if isinstance(ref, str) and ref.startswith("[") and ref.endswith("]") and ref[1:-1] in tools.ledger.read_passages:
                raise ValueError("passage_ref is a raw id, without square brackets. Resubmit with "
                                 + canonical_json({"passage_ref": ref[1:-1]}) + "; nothing was saved")
            raise ValueError("unknown passage_ref; copy an exact p reference from an opened passage, not a search preview. "
                             "Available raw ids (no square brackets): " + ", ".join(list(tools.ledger.read_passages)[-12:]))
        if "source_ref" in args and args["source_ref"] != passage.source.final_url:
            raise ValueError("source_ref and passage_ref refer to different sources; nothing saved")
        extra = args.get("additional_passage_refs", [])
        if (not isinstance(extra, list) or len(extra) > 3 or any(not isinstance(row, str) for row in extra)
                or len(set([ref, *extra])) != 1 + len(extra)):
            raise ValueError("additional_passage_refs requires up to 3 distinct raw passage ids")
        passages = [passage]
        for identity in extra:
            other = tools.ledger.read_passages.get(identity)
            if (other is None or (other.source.final_url, other.source.text_hash, other.source_text)
                    != (passage.source.final_url, passage.source.text_hash, passage.source_text)):
                raise ValueError("all passages must have been read from the same exact source version; nothing saved")
            passages.append(other)
        conditions = _conditions(args.get("conditions", []))
        comparison = args.get("comparison")
        if comparison is not None:
            keys = {"subject", "factor", "comparator", "endpoint", "direction", "uncertainty"}
            if (not isinstance(comparison, dict) or set(comparison) != keys
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in comparison.values())):
                raise ValueError("comparison requires subject, factor, comparator, endpoint, direction and uncertainty; use unknown when absent")
            for name, value in comparison.items():
                conditions[f"comparison_{name}"] = {"state": "not_extracted"} if value == "unknown" else {
                    "state": "reported", "value": value}
        digest = hashlib.sha256(canonical_json({"session": tools.session_id, "project": tools.project,
            "claim": claim, "passage": ref, **({"additional_passages": extra} if extra else {}),
            "conditions": conditions}).encode()).hexdigest()
        identity = "finding-" + digest[:16]
        if tools.store is None:
            raise ValueError("knowledge store is unavailable")
        existing = tools.store.read_note(identity)
        if existing is not None:
            from codey.knowledge.archive import stable_identity
            from codey.research.references import read_version

            version = read_version(tools.store, stable_identity("claim", identity, claim))
            if (existing.body != claim or existing.sources != [passage.source.final_url]
                    or version is None or version.value.content != claim or version.value.conditions != conditions
                    or version.value.status != "candidate"):
                raise ValueError("finding identity conflict; use knowledge_commit for an explicit revision")
            # Recover a committed write without changing its note revision,
            # source acquisition time, or original knowledge transaction.
            for selected in passages:
                item = EvidenceItem(claim, selected.source.final_url, selected.excerpt.strip(),
                                    note_id=identity, locator="passage:" + selected.ref,
                                    page=selected.page)
                if item not in tools.ledger.evidence_items:
                    tools.ledger.add_evidence_items([item])
            if identity not in tools.created_ids and identity not in tools.updated_ids:
                tools.updated_ids.append(identity)
            return _saved(tools, identity, claim, ref, extra, question)
        # A retry reuses the same exact input identity; no fuzzy deduplication.
        lowered: dict[str, Any] = {"type": "fact", "title": claim[:160], "body": claim,
            "sources": [passage.source.final_url], "conditions": conditions,
            "evidence": [{"claim": claim, "source_url": passage.source.final_url,
                          "excerpt": selected.excerpt.strip(), "stance": "supports",
                          "page": selected.page} for selected in passages]}
        # Archive against the frozen source observation selected by the caller,
        # even when the same URL has subsequently changed. Do not change the
        # live ledger's latest source view.
        frozen = ResearchLedger()
        frozen.merge_opened_source(passage.source, passage.source_text, dict(passage.pages))
        outcome = tools.knowledge_write(lowered, finding_id=identity,
                                        evidence_locators=tuple("passage:" + row.ref for row in passages),
                                        evidence_context=frozen)
        if not outcome.ok:
            return outcome
        return _saved(tools, identity, claim, ref, extra, question)
    except (ValueError, TypeError, KeyError) as exc:
        return ResearchToolOutput(f"ERROR: {exc}", ok=False)


def _saved(tools: ResearchTools, identity: str, claim: str, ref: object, extra: list[str], question: str) -> ResearchToolOutput:
    from codey.knowledge.archive import stable_identity
    from codey.research.references import bind_version, read_version
    from codey.research.tools import ResearchToolOutput

    version = read_version(tools.store, stable_identity("claim", identity, claim))
    if version is None:
        raise ValueError("saved finding has no exact claim version")
    label = bind_version(tools.ledger, version, acquisition="current_run", note_id=identity,
                         passage_ref=str(ref), question_ref=question)
    return ResearchToolOutput(canonical_json({"finding_ref": label, "claim": claim,
        "passage_ref": ref, "additional_passage_refs": extra, "interpretation_review": "unreviewed",
        "next": "Cite [" + label + "] only where you adopt this finding. Saving is not semantic approval."}), ok=True)


def _conditions(raw: object) -> dict[str, dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > 24:
        raise ValueError("conditions must be an array of at most 24 {name,state,value?} objects")
    conditions = {}
    for row in raw:
        if (not isinstance(row, dict) or set(row) - {"name", "state", "value"}
                or not isinstance(row.get("name"), str) or not row["name"].strip()
                or row["name"] in conditions):
            raise ValueError("condition needs a distinct nonempty name, state and optional value")
        conditions[row["name"]] = {k: v for k, v in row.items() if k != "name"}
    # Reuse the authoritative condition validator; no invented defaults.
    from codey.knowledge.model import KnowledgeObject

    KnowledgeObject("condition-validation", "claim", "validation", "model_extraction", conditions=conditions)
    return conditions
