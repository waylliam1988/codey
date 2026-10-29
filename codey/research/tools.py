"""Bounded research tools."""

from __future__ import annotations

import contextlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.concept_schema import clean_relations, normalize_concept
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, canonical_json
from codey.knowledge.note import LINK_KINDS, NOTE_STATUSES, NOTE_TYPES, KnowledgeNote, is_safe_id
from codey.knowledge.recall import KnowledgeAccess, KnowledgeHit, render_recall
from codey.knowledge.store import KnowledgeLinkResult, KnowledgeStore
from codey.research.ledger import EvidencePreparation, ResearchLedger
from codey.research.source_gateway import (
    OPEN_DEFAULT_LIMIT,
    OPEN_MAX_LIMIT,
    OPEN_MIN_LIMIT,
    SEARCH_LIMIT,
    ResearchSourceGateway,
)
from codey.research.source_rendering import render_opened_source
from codey.research.source_search import (
    SOURCE_SEARCH_DEFAULT_LIMIT,
    render_results,
)
from codey.research.url_selection import source_candidate_skip_reason
from codey.utils.text_budget import clip_middle

_CITED_TYPES = {"fact", "conclusion", "decision", "implementation", "verification", "synthesis", "project_note"}


@dataclass(frozen=True)
class ResearchToolOutput:
    """Model-visible text plus the full text reserved for durable receipts."""

    model_text: str
    receipt_text: str = ""
    ok: bool = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise TypeError("research output ok must be an exact boolean")


@dataclass
class ResearchTools:
    search: Any
    store: Any
    changes: Any
    diagnostics: object | None = None
    session_id: str = ""
    project: str = ""
    sources_read: set[str] = field(default_factory=set)
    search_result_urls: set[str] = field(default_factory=set)
    grounded_ids: set[str] = field(default_factory=set)
    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    synthesis_id: str = ""
    links_created: int = 0
    ledger: ResearchLedger = field(default_factory=ResearchLedger)

    def create_staged(self) -> ResearchTools:
        staged_store = StagedKnowledgeStore(self.store) if self.store is not None else None
        staged_changes = StagedKnowledgeChanges(self.changes) if self.changes is not None else None
        return ResearchTools(
            search=self.search,
            store=staged_store,
            changes=staged_changes,
            diagnostics=self.diagnostics,
            session_id=self.session_id,
            project=self.project,
            sources_read=set(self.sources_read),
            search_result_urls=set(self.search_result_urls),
            grounded_ids=set(self.grounded_ids),
            created_ids=list(self.created_ids),
            updated_ids=list(self.updated_ids),
            synthesis_id=self.synthesis_id,
            links_created=self.links_created,
            ledger=self.ledger.clone(),
        )

    def commit_staged(self, staged: ResearchTools) -> None:
        if isinstance(staged.store, StagedKnowledgeStore):
            if self.store is None:
                raise ValueError("cannot commit knowledge into an unavailable store")
            staged.store.commit_to(self.store, changes=self.changes)
        self.sources_read.update(staged.sources_read)
        self.search_result_urls.update(staged.search_result_urls)
        self.grounded_ids.update(staged.grounded_ids)
        self.created_ids = list(dict.fromkeys(self.created_ids + staged.created_ids))
        self.updated_ids = list(dict.fromkeys(self.updated_ids + staged.updated_ids))
        if staged.synthesis_id:
            self.synthesis_id = staged.synthesis_id
        self.links_created = max(self.links_created, staged.links_created)
        self.ledger = staged.ledger

    @property
    def gateway(self) -> ResearchSourceGateway:
        """Acquisition spine over this tool's provider and ledger.

        Built fresh per access (no I/O): staged clones pick up their own
        ledger automatically, so staging semantics never drift.
        """
        return ResearchSourceGateway(
            search_provider=self.search,
            ledger=self.ledger,
            on_failure=self._record_failure,
        )

    def web_search(self, query: str) -> ResearchToolOutput:
        query = (query or "").strip()
        if not query:
            return ResearchToolOutput("ERROR: web_search needs a non-empty query", ok=False)
        outcome = self.gateway.search(query, SEARCH_LIMIT)
        if outcome.error:
            return ResearchToolOutput(f"ERROR: {outcome.error}", ok=False)
        results = outcome.hits
        if not results:
            return ResearchToolOutput("no results", ok=True)
        lines: list[str] = []
        skipped = 0
        for r in results:
            url = str(r.get("url") or "")
            if url:
                self.search_result_urls.add(url)
            if source_candidate_skip_reason(url):
                skipped += 1
                continue
            snippet = _clip_tail(str(r.get("snippet") or ""), 200)
            i = len(lines) + 1
            lines.append(f"{i}. {r.get('title')}\n   {url}\n   {snippet}".rstrip())
        if not lines and skipped:
            return ResearchToolOutput(f"no useful non-landing results; skipped {skipped} low-value landing result(s)", ok=True)
        return ResearchToolOutput("\n".join(lines), ok=True)

    def open_url(self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT, pages: str = "") -> ResearchToolOutput:
        """Open a page once: bounded window for the model, full text for receipts."""
        return self._open_document(url, offset=offset, limit=limit, pages=pages)

    def _open_document(
        self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT, pages: str = ""
    ) -> ResearchToolOutput:
        outcome = self.gateway.open(url, offset=offset, limit=limit, pages=pages)
        if outcome.status == "error":
            return ResearchToolOutput(f"ERROR: {outcome.detail}", ok=False)
        if outcome.status == "skipped":
            return ResearchToolOutput(f"SKIPPED: {outcome.detail}", ok=False)
        document = outcome.document
        assert document is not None
        self.sources_read.update(outcome.read_urls)
        offset = max(0, _as_int(offset, 0))
        limit = min(OPEN_MAX_LIMIT, max(OPEN_MIN_LIMIT, _as_int(limit, OPEN_DEFAULT_LIMIT)))
        window = document.text[offset : offset + limit]
        more = offset + limit < len(document.text)
        body = render_opened_source(
            document,
            window,
            more_offset=(offset + limit) if more else None,
        )
        if len(body) > OPEN_MAX_LIMIT:
            body, _truncated = clip_middle(body, OPEN_MAX_LIMIT)
        full = render_opened_source(document, document.text)
        return ResearchToolOutput(model_text=body, receipt_text=full, ok=True)

    def source_search(self, url: str, query: str, limit: object = SOURCE_SEARCH_DEFAULT_LIMIT) -> ResearchToolOutput:
        outcome = self.gateway.search_inside(url, query, limit)
        if outcome.status == "needs_open":
            return ResearchToolOutput(f"NEEDS_OPEN: {outcome.detail}", ok=False)
        if outcome.status == "error":
            return ResearchToolOutput(f"ERROR: {outcome.detail}", ok=False)
        return ResearchToolOutput(render_results(list(outcome.hits)), ok=True)

    def knowledge_search(self, query: str, *, depth: int = 1) -> ResearchToolOutput:
        if self.store is None:
            return ResearchToolOutput("ERROR: knowledge store is unavailable", ok=False)
        query = (query or "").strip()
        if not query:
            return ResearchToolOutput("ERROR: knowledge_search needs a query", ok=False)
        hits = self.store.search(query, project=self.project, session_id=self.session_id, depth=depth)
        recalled = render_recall(hits)
        rows = self.store.search_notes(query, project=self.project, session_id=self.session_id)
        if not rows:
            return ResearchToolOutput(recalled or "no local notes yet on this topic", ok=True)
        lines = [recalled] if recalled else []
        for row in rows:
            status = row.get("status") or "active"
            snippet = _clip_tail(str(row.get("snippet") or ""), 140)
            lines.append(f"[{row['type']}] {row['title']} (id={row['id']}, {status}; legacy document, verify evidence)\n   {snippet}".rstrip())
        return ResearchToolOutput("\n".join(lines), ok=True)

    def knowledge_read(self, note_id: str, *, revision: int | None = None) -> ResearchToolOutput:
        if self.store is None:
            return ResearchToolOutput("ERROR: knowledge store is unavailable", ok=False)
        version = self.store.repository.read(note_id, revision)
        if version is not None:
            if not KnowledgeAccess(self.project, self.session_id, self.store.owner_id).allows(version):
                return ResearchToolOutput("ERROR: knowledge scope mismatch", ok=False)
            payload = version.to_dict()
            payload["content_complete"] = len(version.value.content) <= 12000
            payload["content"] = version.value.content[:12000]
            return ResearchToolOutput(canonical_json(payload), ok=True)
        note = self.store.read_note((note_id or "").strip(), **({"revision": revision} if revision is not None else {}))
        if note is None:
            hints: list[KnowledgeHit] = (
                self.store.search(note_id[:16], project=self.project, session_id=self.session_id, depth=0, limit=3)
                if len(note_id) >= 24 else [])
            suggestion = ("\nPossible exact ids (not read or selected; verify with knowledge_read):\n" +
                "\n".join(canonical_json({"title": hit.version.value.title, "read_request": {
                    "tool": "knowledge_read", "args": {"id": hit.version.value.id, "revision": hit.version.revision}}})
                    for hit in hints)) if hints else ""
            return ResearchToolOutput("ERROR: requested knowledge identity was not found at the requested version. "
                "Copy an exact id and revision from knowledge_search; do not guess or shorten it." + suggestion, ok=False)
        if (note.project and note.project != self.project
                or not note.project and note.session_id and note.session_id != self.session_id):
            return ResearchToolOutput("ERROR: knowledge scope mismatch", ok=False)
        if note.type == "source" and any(s in self.sources_read for s in note.sources):
            self.grounded_ids.add(note.id)
        references: list[dict[str, Any]] = self.store.note_references(note.id, revision) if hasattr(self.store, "note_references") else []
        return ResearchToolOutput(note.to_markdown() + "\n\nStructured knowledge versions (use these for knowledge_refs):\n"
                                  + canonical_json(references), ok=True)

    def knowledge_commit(self, args: dict[str, Any]) -> ResearchToolOutput:
        """The common tool dispatcher owns this effect; the repository only commits."""
        if self.store is None or isinstance(self.store, StagedKnowledgeStore):
            return ResearchToolOutput("ERROR: durable knowledge commit requires the authoritative store", ok=False)
        try:
            raw = args.get("changes")
            if not isinstance(raw, list) or not raw:
                raise ValueError("changes must be a nonempty list of {value, expected_revision}")
            mutations: list[KnowledgeMutation] = []
            access = KnowledgeAccess(self.project, self.session_id, self.store.owner_id)
            for row in raw:
                if not isinstance(row, dict) or set(row) != {"value", "expected_revision"}:
                    raise ValueError("each change needs value and expected_revision")
                value = KnowledgeObject.from_dict(row["value"])
                from codey.knowledge.model import KnowledgeVersion

                if not access.allows(KnowledgeVersion(value, 1, "", "")):
                    raise ValueError("knowledge scope mismatch")
                old = self.store.repository.read(value.id)
                if old is not None and not access.allows(old):
                    raise ValueError("knowledge scope mismatch")
                mutations.append(KnowledgeMutation(value, row["expected_revision"]))
            commit_id = str(args.get("commit_id") or "").strip()
            if not commit_id:
                raise ValueError("commit_id required")
            identity = f"tool:{self.session_id}:{commit_id}"
            receipt = self.store.repository.committed(identity, mutations)
            if receipt is None:
                for mutation in mutations:
                    value = mutation.value
                    if value.kind == "source":
                        url = str(value.provenance.get("url") or "")
                        old = self.store.repository.read(value.id)
                        unchanged_content = old and old.value.content == value.content and old.value.provenance == value.provenance
                        if not unchanged_content and (
                            self.ledger.source_record_for_url(url) is None or value.content != self.ledger.source_text_for_url(url)
                        ):
                            raise ValueError("source content must match an opened source")
            receipt = self.store.commit_knowledge(identity, mutations, changes=self.changes,
                                                   session_id=self.session_id, project=self.project)
            return ResearchToolOutput(canonical_json({"commit_id": commit_id, "versions": receipt}), ok=True)
        except (ValueError, TypeError, KeyError) as exc:
            return ResearchToolOutput(f"ERROR: {exc}", ok=False)

    def knowledge_write(self, args: dict[str, Any]) -> ResearchToolOutput:
        if self.store is None:
            return ResearchToolOutput("ERROR: knowledge store is unavailable", ok=False)
        existing_id, existing_note, updating, identity_error = _resolve_write_target(self.store, args)
        if identity_error:
            return ResearchToolOutput(identity_error, ok=False)
        note_type, title, body, basis_error = _validate_write_basis(args, existing_note)
        if basis_error:
            return ResearchToolOutput(basis_error, ok=False)

        ownership_error = _validate_write_ownership(existing_note, self.session_id, self.project)
        if ownership_error:
            return ResearchToolOutput(ownership_error, ok=False)

        sources, sources_error = _prepare_write_sources(self, args, existing_note, updating, note_type)
        if sources_error:
            return ResearchToolOutput(sources_error, ok=False)
        if note_type in _CITED_TYPES and any(_looks_like_url(source) for source in sources) and not args.get("evidence"):
            return ResearchToolOutput(
                'ERROR: sourced findings require explicit evidence=[{"claim":"supported conclusion",'
                '"source_url":"opened URL","excerpt":"exact contiguous source text","stance":"supports"}]. '
                'Use source_search to locate a passage, then copy it exactly. '
                'A URL alone is not evidence; no inferred quote or supporting claim was saved.', ok=False)
        evidence_preparation = self.ledger.prepare_evidence_items(
            args.get("evidence"),
            fallback_sources=sources,
            fallback_claim=title,
            fallback_body=body,
            note_type=note_type,
        )
        if evidence_preparation.error:
            return ResearchToolOutput(f"ERROR: {evidence_preparation.error}", ok=False)
        relations, relation_warnings = _prepare_write_relations(args, existing_note)
        status = _resolve_write_status(args, existing_note)
        tags = (
            _as_str_list(args.get("tags"))
            if "tags" in args
            else list(existing_note.tags if existing_note is not None else [])
        )
        note = _build_write_note(
            args,
            existing_note,
            existing_id,
            note_type,
            title,
            body,
            sources,
            relations,
            tags,
            status,
            self.session_id,
            self.project,
        )
        from codey.research.knowledge_archive import note_knowledge

        try:
            commit_id, mutations = note_knowledge(note, self.ledger, evidence_preparation, self.store.repository,
                                                 conditions=args.get("conditions"))
        except (ValueError, TypeError) as exc:
            return ResearchToolOutput(f"ERROR: {exc}", ok=False)
        rel = self.store.publish_note(note, commit_id, mutations, changes=self.changes)
        model_text = _finalize_write_note(
            self,
            note,
            note_type,
            evidence_preparation,
            updating,
            relation_warnings,
            rel,
        )
        versions = [{"id": mutation.value.id, "revision": mutation.expected_revision + 1,
                     "kind": mutation.value.kind, "status": mutation.value.status} for mutation in mutations]
        versions.sort(key=lambda row: 0 if row["kind"] in {"claim", "inference", "question"} else 1)
        model_text += "\nStructured knowledge versions:\n" + canonical_json(versions[:24])
        model_text += "\nFor knowledge_read and done.knowledge_refs use these object ids and revisions, not note_id."
        return ResearchToolOutput(model_text, ok=True)

    def knowledge_link(self, src: str, dst: str, kind: str = "relates") -> ResearchToolOutput:
        if self.store is None:
            return ResearchToolOutput("ERROR: knowledge store is unavailable", ok=False)
        src = (src or "").strip()
        dst = (dst or "").strip()
        if not src or not dst:
            return ResearchToolOutput("ERROR: knowledge_link needs src and dst", ok=False)
        result = self.store.link(src, dst, (kind or "relates").strip().lower(), changes=self.changes)
        if result.ok and result.created:
            self.links_created += 1
        return ResearchToolOutput(result.model_text, ok=result.ok)

    def _source_problem(self, sources: list[str]) -> str | None:
        urls = [s for s in sources if _looks_like_url(s)]
        if not urls:
            return "a source note must cite the url of a page you opened"
        unopened = [u for u in urls if u not in self.sources_read]
        if unopened:
            if all(u in self.search_result_urls for u in unopened):
                return "NEEDS_OPEN: open the source before saving this source note: " + ", ".join(unopened[:3])
            return "cite only pages you actually opened; you did not open: " + ", ".join(unopened[:3])
        return None

    def _record_failure(self, area: str, action: str, error: object, *, url: str = "") -> None:
        if self.diagnostics is not None:
            with contextlib.suppress(Exception):
                recorder = getattr(self.diagnostics, "record", None)
                if callable(recorder):
                    recorder(area, action, error, url=url, model=getattr(self.search, "name", ""))

    def _provenance_problem(self, note_type: str, sources: list[str]) -> str | None:
        if not sources:
            opened = sorted(self.sources_read)[:6]
            return (
                f"a {note_type} must cite at least one source you actually read. "
                "Supply sources=[opened URL] and evidence=[{claim:the supported conclusion,"
                "source_url:the same URL,excerpt:exact source text,stance:supports}]. "
                + ("Already opened URLs: " + ", ".join(opened) + ". " if opened else "Open a source page first. ")
                + "Do not change a sourced finding to hypothesis to bypass its missing citation."
            )
        unread_urls = [s for s in sources if _looks_like_url(s) and s not in self.sources_read]
        if unread_urls:
            if all(u in self.search_result_urls for u in unread_urls):
                return "NEEDS_OPEN: open the source before saving this note: " + ", ".join(unread_urls[:3])
            return "cite only pages you actually opened; you did not open: " + ", ".join(unread_urls[:3])
        ungrounded = [s for s in sources if not _looks_like_url(s) and s not in self.grounded_ids]
        if ungrounded:
            return "cite only sources you read; these are not grounded: " + ", ".join(ungrounded[:3])
        return None


def _resolve_write_target(
    store: KnowledgeStore,
    args: dict[str, Any],
) -> tuple[str, KnowledgeNote | None, bool, str | None]:
    existing_id = str(args.get("id") or "").strip()
    if existing_id and not is_safe_id(existing_id):
        return ("", None, False, "ERROR: invalid note id")
    existing_note = store.read_note(existing_id) if existing_id else None
    updating = existing_note is not None
    if existing_id and not updating:
        existing_id = ""
    return (existing_id, existing_note, updating, None)


def _validate_write_basis(
    args: dict[str, Any],
    existing_note: KnowledgeNote | None,
) -> tuple[str, str, str, str | None]:
    note_type = str(
        args.get("type") or (existing_note.type if existing_note is not None else "note")
    ).strip().lower()
    if note_type not in NOTE_TYPES:
        return ("", "", "", f"ERROR: unknown note type '{note_type}'; use one of {', '.join(NOTE_TYPES)}")
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    if not title or not body:
        return ("", "", "", "ERROR: knowledge_write needs both a title and a body")
    return (note_type, title, body, None)


def _validate_write_ownership(
    existing_note: KnowledgeNote | None,
    session_id: str,
    project: str,
) -> str | None:
    if existing_note is not None:
        if existing_note.session_id and session_id and existing_note.session_id != session_id:
            return "ERROR: note belongs to another session"
        if existing_note.project and project and existing_note.project != project:
            return "ERROR: note belongs to another project"
    return None


def _format_write_problem(problem: str | None) -> str | None:
    if problem:
        return problem if problem.startswith("NEEDS_OPEN:") else f"ERROR: {problem}"
    return None


def _prepare_write_sources(
    tools: ResearchTools,
    args: dict[str, Any],
    existing_note: KnowledgeNote | None,
    updating: bool,
    note_type: str,
) -> tuple[list[str], str | None]:
    sources: list[str] = (
        _as_str_list(args.get("sources"))
        if "sources" in args
        else list(existing_note.sources if existing_note is not None else [])
    )
    # A new note can catalogue the URLs the caller already supplied on its
    # evidence items. This copies explicit locators, never invents evidence.
    if "sources" not in args and existing_note is None and isinstance(args.get("evidence"), list):
        sources = list(dict.fromkeys(
            item["source_url"].strip() for item in args["evidence"]
            if isinstance(item, dict) and isinstance(item.get("source_url"), str) and item["source_url"].strip()
        ))
    validating_sources = not updating or "sources" in args
    if note_type == "source":
        problem = (
            tools._source_problem(sources)
            if validating_sources
            else (None if sources else "a source note must cite the url of a page you opened")
        )
        error = _format_write_problem(problem)
        if error:
            return ([], error)
    elif note_type in _CITED_TYPES:
        problem = (
            tools._provenance_problem(note_type, sources)
            if validating_sources
            else (None if sources else f"a {note_type} note must cite at least one opened source URL")
        )
        error = _format_write_problem(problem)
        if error:
            return ([], error)
    return (sources, None)


def _prepare_write_relations(
    args: dict[str, Any],
    existing_note: KnowledgeNote | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if "relations" in args:
        return clean_relations(args.get("relations"))
    return (
        list(existing_note.relations if existing_note is not None else []),
        [],
    )


def _resolve_write_status(args: dict[str, Any], existing_note: KnowledgeNote | None) -> str:
    status = str(
        args.get("status")
        if "status" in args
        else (existing_note.status if existing_note is not None else "active")
    ).strip().lower()
    if status not in NOTE_STATUSES:
        status = "active"
    return status


def _build_write_note(
    args: dict[str, Any],
    existing_note: KnowledgeNote | None,
    existing_id: str,
    note_type: str,
    title: str,
    body: str,
    sources: list[str],
    relations: list[dict[str, Any]],
    tags: list[str],
    status: str,
    session_id: str,
    project: str,
) -> KnowledgeNote:
    return KnowledgeNote.create(
        type=note_type,
        title=title,
        body=body,
        id=existing_id or None,
        tags=_merge_relation_tags(tags, relations),
        sources=sources,
        aliases=(
            _as_str_list(args.get("aliases"))
            if "aliases" in args
            else list(existing_note.aliases if existing_note is not None else [])
        ),
        relations=relations,
        open_questions=(
            _as_str_list(args.get("open_questions"))
            if "open_questions" in args
            else list(existing_note.open_questions if existing_note is not None else [])
        ),
        confidence=(
            _as_float(args.get("confidence"))
            if "confidence" in args
            else (existing_note.confidence if existing_note is not None else None)
        ),
        status=status,
        retrieved_at=(
            _as_opt_str(args.get("retrieved_at"))
            if "retrieved_at" in args
            else (existing_note.retrieved_at if existing_note is not None else None)
        ),
        valid_until=(
            _as_opt_str(args.get("valid_until"))
            if "valid_until" in args
            else (existing_note.valid_until if existing_note is not None else None)
        ),
        session_id=existing_note.session_id if existing_note is not None else session_id,
        project=existing_note.project if existing_note is not None else project,
        created=existing_note.created if existing_note is not None else "",
    )


def _finalize_write_note(
    tools: ResearchTools,
    note: KnowledgeNote,
    note_type: str,
    evidence_preparation: EvidencePreparation,
    updating: bool,
    relation_warnings: list[str],
    rel: str,
) -> str:
    if note_type == "source":
        tools.grounded_ids.add(note.id)
    if evidence_preparation.items:
        tools.ledger.add_evidence_items(list(evidence_preparation.items), note_id=note.id)
    if updating:
        if note.id not in tools.updated_ids:
            tools.updated_ids.append(note.id)
    elif note.id not in tools.created_ids:
        tools.created_ids.append(note.id)
    output = f"saved {note_type} note id={note.id} at {rel}"
    if evidence_preparation.warning:
        output += f"; WARNING: {evidence_preparation.warning}"
    if relation_warnings:
        output += "; WARNING: relations: " + "; ".join(relation_warnings)
    output += (
        f"; note_id: {note.id}; storage_path: {rel} "
        "(do not use storage_path as note_id; pass note_id exactly to knowledge_read)"
    )
    return output


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and not value.strip().isascii():
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    if number is None or not math.isfinite(number):
        return None
    return number


def _as_opt_str(value: Any) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _merge_relation_tags(tags: list[str], relations: Sequence[object]) -> list[str]:
    """Relation endpoints become tags so the Concept Graph can weight them."""
    known = {normalize_concept(tag) for tag in tags}
    merged = list(tags)
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        src = relation.get("src")
        dst = relation.get("dst")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        for concept in (src, dst):
            if concept not in known:
                known.add(concept)
                merged.append(concept)
    return merged


def _looks_like_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _clip_tail(value: str, limit: int) -> str:
    text = (value or "")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return "..." + text[-(limit - 3) :]


class StagedKnowledgeStore:
    """In-memory staging buffer over KnowledgeStore that defers disk writes until committed."""

    def __init__(self, parent_store: KnowledgeStore) -> None:
        self._parent = parent_store
        self._staged_notes: dict[str, KnowledgeNote] = {}
        self._staged_links: list[tuple[str, str, str]] = []
        self._staged_knowledge: dict[str, tuple[str, list[KnowledgeMutation]]] = {}
        self._staged_bases: dict[str, str | None] = {}

    @property
    def root(self) -> Path:
        return self._parent.root

    @property
    def index(self) -> object:
        return self._parent.index

    @property
    def repository(self) -> Any:
        return self._parent.repository

    @property
    def owner_id(self) -> str:
        return self._parent.owner_id

    def search(self, query: str, **kwargs: Any) -> Any:
        return self._parent.search(query, **kwargs)

    def search_notes(self, query: str, **kwargs: Any) -> Any:
        return self._parent.search_notes(query, **kwargs)

    def publish_note(self, note: KnowledgeNote, commit_id: str, mutations: list[KnowledgeMutation],
                     *, changes: KnowledgeChanges | None = None) -> str:
        previous = self._staged_knowledge.get(note.id)
        if previous is not None:
            from codey.knowledge.archive import stable_identity

            merged = {mutation.value.id: mutation for mutation in previous[1]}
            for mutation in mutations:
                old = merged.get(mutation.value.id)
                if old is not None:
                    if old.expected_revision != mutation.expected_revision:
                        raise ValueError("staged knowledge changed its original revision")
                    value = replace(mutation.value, references=tuple(dict.fromkeys(
                        (*old.value.references, *mutation.value.references))))
                    mutation = KnowledgeMutation(value, mutation.expected_revision)
                merged[mutation.value.id] = mutation
            mutations = list(merged.values())
            commit_id = stable_identity("archive", note.id, canonical_json([
                {"value": m.value.to_dict(), "expected_revision": m.expected_revision} for m in mutations]))
        self._staged_knowledge[note.id] = (commit_id, mutations)
        return self.write_note(note, changes=changes)

    def path_for(self, note: KnowledgeNote) -> Path:
        return self._parent.path_for(note)

    def rel(self, path: Path) -> str:
        return self._parent.rel(path)

    def exists(self, note_id: str) -> bool:
        return note_id in self._staged_notes or self._parent.exists(note_id)

    def resolve(self, target: str) -> str | None:
        target = (target or "").strip()
        if not target:
            return None
        if target in self._staged_notes:
            return target
        parent_id = self._parent.index.resolve(target)
        if parent_id:
            return parent_id
        for note in self._staged_notes.values():
            if note.title == target:
                return note.id
        return None

    def read_note(self, note_id: str) -> KnowledgeNote | None:
        if note_id in self._staged_notes:
            return self._staged_notes[note_id]
        return self._parent.read_note(note_id)

    def write_note(
        self,
        note: KnowledgeNote,
        *,
        changes: KnowledgeChanges | None = None,
    ) -> str:
        if note.id not in self._staged_bases:
            original = self._parent.read_note(note.id)
            self._staged_bases[note.id] = original.to_markdown() if original else None
        self._staged_notes[note.id] = note
        if changes is not None:
            record_staged = getattr(changes, "record_staged_note", None)
            if callable(record_staged):
                record_staged(note)
        path = self._parent.path_for(note)
        return self._parent.rel(path) if path.exists() else f"{note.folder}/{note.id}.md"

    def link(self, src: str, dst: str, kind: str = "relates", *, changes: KnowledgeChanges | None = None) -> KnowledgeLinkResult:
        del changes
        kind = kind if kind in LINK_KINDS else "relates"
        src_id = self.resolve(src)
        if src_id is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown source note: {src}")
        dst_id = self.resolve(dst)
        if dst_id is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown target note: {dst}")
        self._staged_links.append((src_id, dst_id, kind))
        return KnowledgeLinkResult(True, f"linked: {src_id} -> {dst_id} ({kind})", created=True)

    def commit_to(self, target_store: KnowledgeStore, changes: KnowledgeChanges | None = None) -> None:
        changes_snapshot = changes.snapshot() if changes is not None else None
        try:
            with target_store.index.transaction():
                for note in self._staged_notes.values():
                    batch = self._staged_knowledge.get(note.id)
                    original = target_store.read_note(note.id)
                    unchanged = original is not None and replace(original, updated=note.updated).to_markdown() == note.to_markdown()
                    if not unchanged and (original.to_markdown() if original else None) != self._staged_bases[note.id]:
                        raise ValueError("staged document revision conflict")
                    if batch is not None:
                        if unchanged and target_store.repository.committed(batch[0], batch[1]) is not None:
                            continue
                        target_store.publish_note(note, batch[0], batch[1], changes=changes, shared_staged=True)
                        continue
                    if unchanged:
                        continue
                    target_store.write_note(note, changes=changes)
                for src_id, dst_id, kind in self._staged_links:
                    result = target_store.link(src_id, dst_id, kind, changes=changes)
                    if not result.ok:
                        raise ValueError(result.model_text)
        except BaseException:
            if changes is not None and changes_snapshot is not None:
                changes.restore_snapshot(changes_snapshot)
            raise
        self._staged_notes.clear()
        self._staged_links.clear()
        self._staged_knowledge.clear()
        self._staged_bases.clear()


class StagedKnowledgeChanges:
    """In-memory tracking for note file changes during staging without disk side-effects."""

    def __init__(self, parent_changes: KnowledgeChanges | None = None) -> None:
        del parent_changes

    def record_staged_note(self, note: KnowledgeNote) -> None:
        del note

    def capture_before(self, rel: str, path: Path) -> None:
        """No-op during staging: staged notes do not inspect or snapshot disk paths."""
        del rel, path

    def record_after(self, rel: str, path: Path) -> None:
        del rel, path


def clone_research_tools(
    tools: ResearchTools,
    *,
    search: object | None = None,
    diagnostics: object | None = None,
    session_id: str | None = None,
    project: str | None = None,
) -> ResearchTools:
    """Create a fresh tool facade over the same run-scoped research facts."""

    return ResearchTools(
        search=search if search is not None else tools.search,
        store=tools.store,
        changes=tools.changes,
        diagnostics=diagnostics if diagnostics is not None else tools.diagnostics,
        session_id=tools.session_id if session_id is None else session_id,
        project=tools.project if project is None else project,
        sources_read=tools.sources_read,
        search_result_urls=tools.search_result_urls,
        grounded_ids=tools.grounded_ids,
        created_ids=tools.created_ids,
        updated_ids=tools.updated_ids,
        links_created=tools.links_created,
        ledger=tools.ledger,
    )
