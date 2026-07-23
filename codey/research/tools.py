"""Bounded research tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey import cancellation
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import NOTE_STATUSES, NOTE_TYPES, KnowledgeNote, is_safe_id
from codey.knowledge.store import KnowledgeStore
from codey.research.ledger import ResearchLedger
from codey.research.url_policy import check_fetch_url
from codey.text_budget import clip_middle

OPEN_DEFAULT_LIMIT = 6000
OPEN_MAX_LIMIT = 12000
SEARCH_LIMIT = 8
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

    def open_url(self, url: str, offset: int = 0, limit: int = OPEN_DEFAULT_LIMIT) -> str:
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
        if page_text.startswith("ERROR:"):
            message = page_text[len("ERROR:"):].strip()
            if message.lower().startswith("unsupported content type:"):
                return f"SKIPPED: {message}. Choose an HTML source or another readable page."
            self._record_failure("browser", "open", message, url=url)
            return f"ERROR: {message}"
        reason = check_fetch_url(page_url)
        if reason:
            return f"ERROR: {reason} (after redirect)"
        self.sources_read.add(url)
        if page_url and page_url != url:
            self.sources_read.add(page_url)
        self.ledger.record_open(
            requested_url=url,
            final_url=page_url,
            title=str(page.get("title") or ""),
            text=page_text,
        )
        window = page_text[offset : offset + limit]
        more = offset + limit < len(page_text)
        header = f"{page.get('title')}\n{page_url}".strip()
        body = f"{header}\n\n{window}"
        if more:
            body += f"\n\n[more text available: open with offset={offset + limit}]"
        if len(body) > OPEN_MAX_LIMIT:
            body, _truncated = clip_middle(body, OPEN_MAX_LIMIT)
        return body

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
            tags=_as_str_list(args.get("tags")),
            sources=sources,
            aliases=_as_str_list(args.get("aliases")),
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
                return "NEEDS_OPEN: open_url before saving this source note: " + ", ".join(unopened[:3])
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
                return "NEEDS_OPEN: open_url before saving this note: " + ", ".join(unread_urls[:3])
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


def _looks_like_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _clip_tail(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[-limit:]
    return "..." + text[-(limit - 3):]
