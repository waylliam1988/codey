"""Bounded research tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codey import cancellation
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.concept_schema import clean_relations, normalize_concept
from codey.knowledge.note import NOTE_STATUSES, NOTE_TYPES, KnowledgeNote, is_safe_id
from codey.knowledge.store import KnowledgeStore
from codey.research.ledger import ResearchLedger
from codey.research.pdf_extract import (
    PDF_DEFAULT_PAGES,
    PDF_MAX_PAGES_PER_OPEN,
    PdfSkipped,
    extract_pdf_document,
)
from codey.research.source_document import SourceDocument, compact_pages
from codey.research.source_search import (
    SOURCE_SEARCH_DEFAULT_LIMIT,
    SourceSearchHit,
    bounded_limit,
    render_results,
    search_pages,
    search_text,
)
from codey.research.url_policy import check_fetch_url
from codey.text_budget import clip_middle

OPEN_DEFAULT_LIMIT = 6000
OPEN_MAX_LIMIT = 12000
SEARCH_LIMIT = 8
PDF_SOURCE_SEARCH_MAX_PAGES = 30
_CITED_TYPES = {"fact", "conclusion", "decision", "implementation", "verification", "synthesis", "project_note"}


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

    def web_search(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "ERROR: web_search needs a non-empty query"
        cancellation.check()
        try:
            results = self.search.search(query, limit=SEARCH_LIMIT)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._record_failure("search", "search", exc)
            return f"ERROR: search failed: {exc}"
        cancellation.check()
        self.ledger.record_search(query, results)
        if not results:
            return "no results"
        lines = []
        for i, r in enumerate(results, 1):
            url = str(r.get("url") or "")
            if url:
                self.search_result_urls.add(url)
            snippet = _clip_tail(str(r.get("snippet") or ""), 200)
            lines.append(f"{i}. {r.get('title')}\n   {url}\n   {snippet}".rstrip())
        return "\n".join(lines)

    def open_url(self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT, pages: str = "") -> str:
        url = (url or "").strip()
        if not url:
            return "ERROR: open_url needs a url"
        offset = max(0, _as_int(offset, 0))
        limit = min(OPEN_MAX_LIMIT, max(500, _as_int(limit, OPEN_DEFAULT_LIMIT)))
        cancellation.check()
        try:
            page = self.search.fetch(url)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._record_failure("browser", "open", exc, url=url)
            return f"ERROR: open failed: {exc}"
        cancellation.check()
        page_url = str(page.get("url") or url)
        page_text = str(page.get("text") or "")
        if page_text.startswith("SKIPPED:"):
            return page_text
        if page_text.startswith("ERROR:"):
            message = page_text[len("ERROR:"):].strip()
            if message.lower().startswith("unsupported content type:"):
                return f"SKIPPED: {message}. Choose an HTML source or another readable page."
            self._record_failure("browser", "open", message, url=url)
            return f"ERROR: {message}"
        reason = check_fetch_url(page_url)
        if reason:
            return f"ERROR: {reason} (after redirect)"
        document = self._source_document_from_fetch(url, page, pages=pages)
        if isinstance(document, PdfSkipped):
            return f"SKIPPED: {document.reason}. Choose an HTML source or another readable PDF."
        self.sources_read.add(url)
        if page_url and page_url != url:
            self.sources_read.add(page_url)
        self.ledger.record_open_document(document)
        window = document.text[offset : offset + limit]
        more = offset + limit < len(document.text)
        header = _document_header(document)
        body = f"{header}\n\n{window}"
        if more:
            body += f"\n\n[more text available: open with offset={offset + limit}]"
        if len(body) > OPEN_MAX_LIMIT:
            body, _truncated = clip_middle(body, OPEN_MAX_LIMIT)
        return body

    def _source_document_from_fetch(self, requested_url: str, page: dict, *, pages: str = "") -> SourceDocument | PdfSkipped:
        final_url = str(page.get("url") or requested_url)
        content_kind = str(page.get("content_kind") or "").lower()
        mime_type = str(page.get("mime_type") or "")
        if content_kind == "pdf":
            return extract_pdf_document(
                bytes(page.get("bytes") or b""),
                requested_url=requested_url,
                final_url=final_url,
                title=str(page.get("title") or ""),
                mime_type=mime_type or "application/pdf",
                pages=pages or PDF_DEFAULT_PAGES,
            )
        return SourceDocument.html(
            requested_url=requested_url,
            final_url=final_url,
            title=str(page.get("title") or ""),
            text=str(page.get("text") or ""),
            mime_type=mime_type or "text/html",
            truncated=bool(page.get("truncated")),
        )

    def source_search(self, url: str, query: str, limit: object = SOURCE_SEARCH_DEFAULT_LIMIT) -> str:
        url = (url or "").strip()
        query = (query or "").strip()
        if not url:
            return "ERROR: source_search needs a url"
        if not query:
            return "ERROR: source_search needs a query"
        final_url = self.ledger.canonical_opened_url(url)
        if not final_url:
            return "NEEDS_OPEN: open the source before source_search: " + url
        source = self.ledger.source_record_for_url(final_url)
        if source is None:
            return "ERROR: source_search source is not in the opened-source ledger"
        hit_limit = bounded_limit(limit)
        cancellation.check()
        if source.content_kind == "pdf":
            hits = search_pages(self.ledger.source_pages_for_url(final_url), query, hit_limit)
            if self._pdf_source_search_scan_needed(source.final_url):
                hits = _merge_source_hits([
                    *hits,
                    *self._pdf_source_search_hits(source.final_url, query, hit_limit),
                ], hit_limit)
        else:
            hits = search_text(self.ledger.source_text_for_url(final_url), query, hit_limit)
        self.ledger.record_source_search(final_url, query, [hit.to_dict() for hit in hits])
        return render_results(final_url, hits)

    def _pdf_source_search_scan_needed(self, final_url: str) -> bool:
        source = self.ledger.source_record_for_url(final_url)
        if source is None or source.content_kind != "pdf":
            return False
        page_count = max(0, int(source.page_count or 0))
        if page_count <= 0:
            return False
        scan_end = min(page_count, PDF_SOURCE_SEARCH_MAX_PAGES)
        pages_read = self.ledger.pages_read_for_url(final_url)
        return any(page not in pages_read for page in range(1, scan_end + 1))

    def _pdf_source_search_hits(self, final_url: str, query: str, limit: int) -> list[SourceSearchHit]:
        source = self.ledger.source_record_for_url(final_url)
        if source is None or source.content_kind != "pdf":
            return []
        page_count = max(0, int(source.page_count or 0))
        if page_count <= 0:
            return []
        scan_end = min(page_count, PDF_SOURCE_SEARCH_MAX_PAGES)
        try:
            page = self.search.fetch(source.final_url)
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self._record_failure("browser", "source_search", exc, url=source.final_url)
            return []
        cancellation.check()
        page_url = str(page.get("url") or source.final_url)
        reason = check_fetch_url(page_url)
        if reason:
            self._record_failure("browser", "source_search", reason, url=source.final_url)
            return []
        if page_url != source.final_url and self.ledger.canonical_opened_url(page_url) != source.final_url:
            self._record_failure(
                "browser",
                "source_search",
                "redirect changed opened source",
                url=source.final_url,
            )
            return []
        data = bytes(page.get("bytes") or b"")
        if not data:
            return []
        page_texts: dict[int, str] = {}
        for start in range(1, scan_end + 1, PDF_MAX_PAGES_PER_OPEN):
            end = min(scan_end, start + PDF_MAX_PAGES_PER_OPEN - 1)
            document = extract_pdf_document(
                data,
                requested_url=source.requested_url or source.final_url,
                final_url=source.final_url,
                title=source.title,
                mime_type=source.mime_type or "application/pdf",
                pages=f"{start}-{end}",
            )
            if isinstance(document, PdfSkipped):
                continue
            page_texts.update({page.number: page.text for page in document.page_texts})
        return search_pages(page_texts, query, limit)

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
            lines.append(
                f"[{row['type']}] {row['title']} (id={row['id']}, {status}{conf})\n   {snippet}".rstrip()
            )
        return "\n".join(lines)

    def knowledge_read(self, note_id: str) -> str:
        note = self.store.read_note((note_id or "").strip())
        if note is None:
            return f"ERROR: no note with id {note_id}"
        if note.type == "source" and any(s in self.sources_read for s in note.sources):
            self.grounded_ids.add(note.id)
        return note.to_markdown()

    def knowledge_write(self, args: dict) -> str:
        note_type = str(args.get("type") or "note").strip().lower()
        if note_type not in NOTE_TYPES:
            return f"ERROR: unknown note type '{note_type}'; use one of {', '.join(NOTE_TYPES)}"
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title or not body:
            return "ERROR: knowledge_write needs both a title and a body"
        sources = _as_str_list(args.get("sources"))
        if note_type == "source":
            problem = self._source_problem(sources)
            if problem:
                return problem if problem.startswith("NEEDS_OPEN:") else f"ERROR: {problem}"
        elif note_type in _CITED_TYPES:
            problem = self._provenance_problem(note_type, sources)
            if problem:
                return problem if problem.startswith("NEEDS_OPEN:") else f"ERROR: {problem}"
        evidence_preparation = self.ledger.prepare_evidence_items(
            args.get("evidence"),
            fallback_sources=sources,
            fallback_claim=title,
            fallback_body=body,
            note_type=note_type,
        )
        if evidence_preparation.error:
            return f"ERROR: {evidence_preparation.error}"
        relations, relation_warnings = clean_relations(args.get("relations"))
        status = str(args.get("status") or "active").strip().lower()
        if status not in NOTE_STATUSES:
            status = "active"
        existing_id = str(args.get("id") or "").strip()
        if existing_id and not is_safe_id(existing_id):
            return "ERROR: invalid note id"
        updating = bool(existing_id and self.store.exists(existing_id))
        if existing_id and not updating:
            existing_id = ""
        note = KnowledgeNote.create(
            type=note_type,
            title=title,
            body=body,
            id=existing_id or None,
            tags=_merge_relation_tags(_as_str_list(args.get("tags")), relations),
            sources=sources,
            aliases=_as_str_list(args.get("aliases")),
            relations=relations,
            open_questions=_as_str_list(args.get("open_questions")),
            confidence=_as_float(args.get("confidence")),
            status=status,
            retrieved_at=_as_opt_str(args.get("retrieved_at")),
            valid_until=_as_opt_str(args.get("valid_until")),
            session_id=self.session_id,
            project=self.project,
        )
        rel = self.store.write_note(note, changes=self.changes)
        if note_type == "source":
            self.grounded_ids.add(note.id)
        if evidence_preparation.items:
            self.ledger.add_evidence_items(list(evidence_preparation.items), note_id=note.id)
        if updating:
            if note.id not in self.updated_ids:
                self.updated_ids.append(note.id)
        elif note.id not in self.created_ids:
            self.created_ids.append(note.id)
        output = f"saved {note_type} note id={note.id} at {rel}"
        if evidence_preparation.warning:
            output += f"; WARNING: {evidence_preparation.warning}"
        if relation_warnings:
            output += "; WARNING: relations: " + "; ".join(relation_warnings)
        return output

    def knowledge_link(self, src: str, dst: str, kind: str = "relates") -> str:
        src = (src or "").strip()
        dst = (dst or "").strip()
        if not src or not dst:
            return "ERROR: knowledge_link needs src and dst"
        result = self.store.link(src, dst, (kind or "relates").strip().lower(), changes=self.changes)
        if result.startswith("ERROR:"):
            return result
        changed = result.startswith(("linked:", "updated link:"))
        if result.startswith("linked:"):
            self.links_created += 1
        return result if changed else result

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
            try:
                self.diagnostics.record(area, action, error, url=url, model=getattr(self.search, "name", ""))
            except Exception:
                pass

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
    return "..." + text[-(limit - 3):]


def _merge_source_hits(hits: list[SourceSearchHit], limit: int) -> list[SourceSearchHit]:
    seen: set[tuple[int | None, int]] = set()
    unique: list[SourceSearchHit] = []
    for hit in sorted(hits, key=lambda item: (-item.score, item.page or 0, item.offset)):
        key = (hit.page, hit.offset)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
        if len(unique) >= limit:
            break
    return unique


def _document_header(document: SourceDocument) -> str:
    lines = [document.title, document.final_url]
    if document.content_kind == "pdf":
        page_meta = _pages_meta(document.pages_read, document.page_count)
        bits = ["PDF", page_meta]
        if document.truncated:
            bits.append("truncated")
        lines.append(" · ".join(part for part in bits if part))
    return "\n".join(str(line or "").strip() for line in lines if str(line or "").strip())


def _pages_meta(pages: tuple[int, ...], page_count: int) -> str:
    if not pages:
        return ""
    page_text = compact_pages(pages)
    return f"pages {page_text} / {page_count}" if page_count else f"pages {page_text}"


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

    def get_note(self, note_id: str) -> KnowledgeNote | None:
        return self.read_note(note_id)

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
        if not self.exists(src):
            return f"ERROR: unknown source note: {src}"
        if not self.exists(dst):
            return f"ERROR: unknown target note: {dst}"
        self._staged_links.append((src, dst, kind))
        return f"linked: {src} -> {dst} ({kind})"

    def commit_to(self, target_store: KnowledgeStore, changes: KnowledgeChanges | None = None) -> None:
        written_files: list[tuple[Path, str | None]] = []
        try:
            for note in self._staged_notes.values():
                path = target_store.path_for(note)
                orig_content = path.read_text(encoding="utf-8") if path.is_file() else None
                written_files.append((path, orig_content))
                target_store.write_note(note, changes=changes)
            for src, dst, kind in self._staged_links:
                target_store.link(src, dst, kind, changes=changes)
            self._staged_notes.clear()
            self._staged_links.clear()
        except Exception:
            # Atomic rollback: revert all notes written to disk during this commit attempt
            for path, orig_content in reversed(written_files):
                try:
                    if orig_content is None:
                        if path.is_file():
                            path.unlink(missing_ok=True)
                    else:
                        path.write_text(orig_content, encoding="utf-8")
                except Exception:
                    pass
            raise



class StagedKnowledgeChanges:
    """In-memory tracking for changes during staging."""

    def __init__(self, parent_changes: KnowledgeChanges | None = None) -> None:
        self._parent = parent_changes
        self._staged_files: set[str] = set()

    def record_staged_note(self, note: KnowledgeNote) -> None:
        self._staged_files.add(f"{note.folder}/{note.id}.md")

    def capture_before(self, rel: str, path: Path) -> None:
        pass

    def record_after(self, rel: str, path: Path) -> None:
        self._staged_files.add(rel)


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
