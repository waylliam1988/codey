"""Bounded research tools."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.concept_schema import clean_relations, normalize_concept
from codey.knowledge.note import LINK_KINDS, NOTE_STATUSES, NOTE_TYPES, KnowledgeNote, is_safe_id
from codey.knowledge.store import KnowledgeStore, content_hash_bytes
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


@dataclass
class ResearchTools:
    search: object
    store: KnowledgeStore
    changes: KnowledgeChanges
    diagnostics: object | None = None
    session_id: str = ""
    project: str = ""
    sources_read: set[str] = field(default_factory=set)
    search_result_urls: set[str] = field(default_factory=set)
    grounded_ids: set[str] = field(default_factory=set)
    created_ids: list[str] = field(default_factory=list)
    updated_ids: list[str] = field(default_factory=list)
    links_created: int = 0
    ledger: ResearchLedger = field(default_factory=ResearchLedger)

    def create_staged(self) -> ResearchTools:
        staged_store = StagedKnowledgeStore(self.store)
        staged_changes = StagedKnowledgeChanges(self.changes)
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
            links_created=self.links_created,
            ledger=self.ledger.clone(),
        )

    def commit_staged(self, staged: ResearchTools) -> None:
        if isinstance(staged.store, StagedKnowledgeStore):
            staged.store.commit_to(self.store, changes=self.changes)
        self.sources_read.update(staged.sources_read)
        self.search_result_urls.update(staged.search_result_urls)
        self.grounded_ids.update(staged.grounded_ids)
        self.created_ids = list(dict.fromkeys(self.created_ids + staged.created_ids))
        self.updated_ids = list(dict.fromkeys(self.updated_ids + staged.updated_ids))
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

    def web_search(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "ERROR: web_search needs a non-empty query"
        outcome = self.gateway.search(query, SEARCH_LIMIT)
        if outcome.error:
            return f"ERROR: {outcome.error}"
        results = outcome.hits
        if not results:
            return "no results"
        lines = []
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
            return f"no useful non-landing results; skipped {skipped} low-value landing result(s)"
        return "\n".join(lines)

    def open_url(self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT, pages: str = "") -> ResearchToolOutput:
        """Open a page once: bounded window for the model, full text for receipts."""
        return self._open_document(url, offset=offset, limit=limit, pages=pages)

    def open_url_text(self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT, pages: str = "") -> str:
        """String-only boundary for callers that need just model-visible text."""
        return self.open_url(url, offset=offset, limit=limit, pages=pages).model_text

    def _open_document(
        self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT, pages: str = ""
    ) -> ResearchToolOutput:
        outcome = self.gateway.open(url, offset=offset, limit=limit, pages=pages)
        if outcome.status == "error":
            return ResearchToolOutput(f"ERROR: {outcome.detail}")
        if outcome.status == "skipped":
            return ResearchToolOutput(f"SKIPPED: {outcome.detail}")
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
        return ResearchToolOutput(model_text=body, receipt_text=full)

    def source_search(self, url: str, query: str, limit: object = SOURCE_SEARCH_DEFAULT_LIMIT) -> str:
        outcome = self.gateway.search_inside(url, query, limit)
        if outcome.status == "needs_open":
            return f"NEEDS_OPEN: {outcome.detail}"
        if outcome.status == "error":
            return f"ERROR: {outcome.detail}"
        return render_results(outcome.final_url, list(outcome.hits))

    def knowledge_search(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "ERROR: knowledge_search needs a query"
        rows = self.store.index.search(query)
        if not rows:
            return "no local notes yet on this topic"
        lines = []
        for row in rows:
            conf = "" if row.get("confidence") is None else f" conf={row['confidence']}"
            status = row.get("status") or "active"
            snippet = _clip_tail(str(row.get("snippet") or ""), 140)
            lines.append(f"[{row['type']}] {row['title']} (id={row['id']}, {status}{conf})\n   {snippet}".rstrip())
        return "\n".join(lines)

    def knowledge_read(self, note_id: str) -> str:
        note = self.store.read_note((note_id or "").strip())
        if note is None:
            return f"ERROR: no note with id {note_id}"
        if note.type == "source" and any(s in self.sources_read for s in note.sources):
            self.grounded_ids.add(note.id)
        return note.to_markdown()

    def knowledge_write(self, args: dict) -> str:
        existing_id, existing_note, updating, identity_error = _resolve_write_target(self.store, args)
        if identity_error:
            return identity_error
        note_type, title, body, basis_error = _validate_write_basis(args, existing_note)
        if basis_error:
            return basis_error

        ownership_error = _validate_write_ownership(existing_note, self.session_id, self.project)
        if ownership_error:
            return ownership_error

        sources, sources_error = _prepare_write_sources(self, args, existing_note, updating, note_type)
        if sources_error:
            return sources_error
        evidence_preparation = self.ledger.prepare_evidence_items(
            args.get("evidence"),
            fallback_sources=sources,
            fallback_claim=title,
            fallback_body=body,
            note_type=note_type,
        )
        if evidence_preparation.error:
            return f"ERROR: {evidence_preparation.error}"
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
        rel = self.store.write_note(note, changes=self.changes)
        return _finalize_write_note(
            self,
            note,
            note_type,
            evidence_preparation,
            updating,
            relation_warnings,
            rel,
        )

    def knowledge_link(self, src: str, dst: str, kind: str = "relates") -> str:
        src = (src or "").strip()
        dst = (dst or "").strip()
        if not src or not dst:
            return "ERROR: knowledge_link needs src and dst"
        result = self.store.link(src, dst, (kind or "relates").strip().lower(), changes=self.changes)
        if result.startswith("ERROR:"):
            return result
        if result.startswith("linked:"):
            self.links_created += 1
        return result

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
                self.diagnostics.record(area, action, error, url=url, model=getattr(self.search, "name", ""))

    def _provenance_problem(self, note_type: str, sources: list[str]) -> str | None:
        if not sources:
            return (
                f"a {note_type} must cite at least one source you actually read; "
                "open the page first, or use type 'hypothesis' for an inference"
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
    args: dict,
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
    args: dict,
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
    args: dict,
    existing_note: KnowledgeNote | None,
    updating: bool,
    note_type: str,
) -> tuple[list[str], str | None]:
    sources = (
        _as_str_list(args.get("sources"))
        if "sources" in args
        else list(existing_note.sources if existing_note is not None else [])
    )
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
    args: dict,
    existing_note: KnowledgeNote | None,
) -> tuple[list[dict], list[str]]:
    if "relations" in args:
        return clean_relations(args.get("relations"))
    return (
        list(existing_note.relations if existing_note is not None else []),
        [],
    )


def _resolve_write_status(args: dict, existing_note: KnowledgeNote | None) -> str:
    status = str(
        args.get("status")
        if "status" in args
        else (existing_note.status if existing_note is not None else "active")
    ).strip().lower()
    if status not in NOTE_STATUSES:
        status = "active"
    return status


def _build_write_note(
    args: dict,
    existing_note: KnowledgeNote | None,
    existing_id: str,
    note_type: str,
    title: str,
    body: str,
    sources: list[str],
    relations: list[dict],
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
    return output


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_opt_str(value) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


def _as_str_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _merge_relation_tags(tags: list[str], relations: list[dict]) -> list[str]:
    """Relation endpoints become tags so the Concept Graph can weight them."""
    known = {normalize_concept(tag) for tag in tags}
    merged = list(tags)
    for relation in relations:
        for concept in (relation["src"], relation["dst"]):
            if concept not in known:
                known.add(concept)
                merged.append(concept)
    return merged


def _looks_like_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _clip_tail(value: str, limit: int) -> str:
    text = str(value or "")
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

    @property
    def root(self) -> Path:
        return self._parent.root

    @property
    def index(self) -> object:
        return self._parent.index

    def path_for(self, note: KnowledgeNote) -> Path:
        return self._parent.path_for(note)

    def rel(self, path: Path) -> str:
        return self._parent.rel(path)

    def exists(self, note_id: str) -> bool:
        return note_id in self._staged_notes or self._parent.exists(note_id)

    def resolve(self, target: str) -> str | None:
        target = str(target or "").strip()
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
        self._staged_notes[note.id] = note
        if changes is not None:
            record_staged = getattr(changes, "record_staged_note", None)
            if callable(record_staged):
                record_staged(note)
        path = self._parent.path_for(note)
        return self._parent.rel(path) if path.exists() else f"{note.folder}/{note.id}.md"

    def link(self, src: str, dst: str, kind: str = "relates", *, changes: KnowledgeChanges | None = None) -> str:
        del changes
        kind = kind if kind in LINK_KINDS else "relates"
        src_id = self.resolve(src)
        if src_id is None:
            return f"ERROR: unknown source note: {src}"
        dst_id = self.resolve(dst)
        if dst_id is None:
            return f"ERROR: unknown target note: {dst}"
        self._staged_links.append((src_id, dst_id, kind))
        return f"linked: {src_id} -> {dst_id} ({kind})"

    def commit_to(self, target_store: KnowledgeStore, changes: KnowledgeChanges | None = None) -> None:
        changes_snapshot = changes.snapshot() if changes is not None else None

        # Track written/modified notes and files for compensating rollback:
        # note_id -> (new_path, orig_path_or_None, orig_bytes_or_None, orig_note_or_None)
        tracked_notes: dict[str, tuple[Path, Path | None, bytes | None, KnowledgeNote | None]] = {}
        link_touch_ids = set(self._staged_notes)
        for src_id, dst_id, _ in self._staged_links:
            link_touch_ids.update((src_id, dst_id))
        saved_links = target_store.index.links_touching(sorted(link_touch_ids))
        try:
            for note in self._staged_notes.values():
                new_path = target_store.path_for(note)
                orig_existed = target_store.exists(note.id)
                orig_note = target_store.read_note(note.id) if orig_existed else None
                orig_path = target_store.path_for(orig_note) if orig_note is not None else None
                orig_bytes = orig_path.read_bytes() if (orig_path and orig_path.is_file()) else None
                tracked_notes[note.id] = (new_path, orig_path, orig_bytes, orig_note)
                target_store.write_note(note, changes=changes)

            for src_id, dst_id, kind in self._staged_links:
                if src_id not in tracked_notes:
                    orig_existed = target_store.exists(src_id)
                    orig_src_note = target_store.read_note(src_id) if orig_existed else None
                    if orig_src_note is not None:
                        src_path = target_store.path_for(orig_src_note)
                        src_bytes = src_path.read_bytes() if src_path.is_file() else None
                        tracked_notes[src_id] = (src_path, src_path, src_bytes, orig_src_note)
                target_store.link(src_id, dst_id, kind, changes=changes)

            self._staged_notes.clear()
            self._staged_links.clear()
        except Exception as exc:
            # Compensating rollback: revert all notes, index entries, links, and changes
            cleanup_errors: list[str] = []
            for note_id, (new_path, orig_path, orig_bytes, orig_note) in reversed(list(tracked_notes.items())):
                try:
                    if orig_note is None:
                        # 1. New note created during this commit attempt
                        if new_path.is_file():
                            new_path.unlink(missing_ok=True)
                        target_store.index.remove(note_id)
                    else:
                        # 2. Existing note modified or moved during this commit attempt
                        # If folder/type changed, delete the newly created path file
                        if orig_path is not None and new_path != orig_path and new_path.is_file():
                            new_path.unlink(missing_ok=True)
                        # Byte-level exact restoration of original markdown file (no timestamp bump)
                        if orig_path is not None and orig_bytes is not None:
                            orig_path.parent.mkdir(parents=True, exist_ok=True)
                            orig_path.write_bytes(orig_bytes)
                        # Exact index & links restoration to original snapshot
                        if orig_path is not None:
                            rel = target_store.rel(orig_path)
                            c_hash = content_hash_bytes(orig_bytes) if orig_bytes is not None else ""
                            target_store.index.upsert(orig_note, path=rel, content_hash=c_hash)
                            target_store._index_body_links(orig_note)
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"{note_id}: {cleanup_exc}")

            try:
                target_store.index.replace_links_touching(sorted(link_touch_ids), saved_links)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"links: {cleanup_exc}")

            if changes is not None and changes_snapshot is not None:
                changes.restore_snapshot(changes_snapshot)
            if cleanup_errors:
                exc.add_note("staged knowledge rollback cleanup errors: " + "; ".join(cleanup_errors[:6]))
            raise


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
