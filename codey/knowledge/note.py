"""Markdown knowledge notes.

The user-facing feature is Research, but the internal store is broader than
research reports: decisions, implementation receipts, and verification evidence
all become small Markdown artifacts with explicit provenance.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from codey.knowledge.concept_schema import clean_relations
from codey.policies.prompt_safety import is_prompt_visible_text_safe

NOTE_TYPES = (
    "source",
    "fact",
    "hypothesis",
    "conclusion",
    "question",
    "synthesis",
    "decision",
    "implementation",
    "verification",
    "project_note",
    "commit",
    "note",
)
NOTE_STATUSES = ("active", "stale", "contradicted", "superseded")
LINK_KINDS = ("relates", "supports", "contradicts", "derives", "implements", "verifies")
MAX_OPEN_QUESTIONS = 12
MAX_OPEN_QUESTION_CHARS = 240

_TYPE_FOLDERS = {
    "source": "sources",
    "fact": "facts",
    "hypothesis": "hypotheses",
    "conclusion": "conclusions",
    "question": "questions",
    "synthesis": "synthesis",
    "decision": "decisions",
    "implementation": "implementation",
    "verification": "verification",
    "project_note": "projects",
    "commit": "commits",
    "note": "notes",
}
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
_WIKILINK_KIND_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\](?:\s*\(([a-z_]+)\))?")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]*$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def slugify(title: str, limit: int = 48) -> str:
    text = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    return (text[:limit].strip("-")) or "note"


def make_id(title: str) -> str:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{slugify(title)}-{uuid.uuid4().hex[:8]}"


def is_safe_id(note_id: str) -> bool:
    note_id = (note_id or "").strip()
    if not note_id or len(note_id) > 200:
        return False
    if ".." in note_id or "/" in note_id or "\\" in note_id:
        return False
    return bool(_SAFE_ID_RE.match(note_id))


@dataclass
class KnowledgeNote:
    id: str
    type: str = "note"
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    confidence: float | None = None
    status: str = "active"
    session_id: str = ""
    project: str = ""
    retrieved_at: str | None = None
    valid_until: str | None = None
    created: str = ""
    updated: str = ""

    def __post_init__(self) -> None:
        if self.type not in NOTE_TYPES:
            self.type = "note"
        self.relations = clean_relations(self.relations)[0]
        self.open_questions = clean_open_questions(self.open_questions)
        if self.status not in NOTE_STATUSES:
            self.status = "active"
        if not self.created:
            self.created = now_iso()
        if not self.updated:
            self.updated = self.created

    @classmethod
    def create(cls, *, type: str, title: str, body: str, **kwargs) -> "KnowledgeNote":
        note_id = kwargs.pop("id", None) or make_id(title)
        return cls(id=note_id, type=type, title=title, body=body, **kwargs)

    @property
    def folder(self) -> str:
        return _TYPE_FOLDERS.get(self.type, "notes")

    def wikilink_edges(self) -> list[tuple[str, str]]:
        edges: dict[str, str] = {}
        for match in _WIKILINK_KIND_RE.finditer(self.body):
            target = match.group(1).strip()
            if not target:
                continue
            kind = match.group(2)
            kind = kind if kind in LINK_KINDS else "relates"
            if target not in edges or (edges[target] == "relates" and kind != "relates"):
                edges[target] = kind
        return list(edges.items())

    def frontmatter(self) -> dict:
        data: dict = {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
        }
        if self.tags:
            data["tags"] = list(self.tags)
        if self.sources:
            data["sources"] = list(self.sources)
        if self.aliases:
            data["aliases"] = list(self.aliases)
        if self.relations:
            data["relations"] = [dict(r) for r in self.relations]
        if self.open_questions:
            data["open_questions"] = list(self.open_questions)
        if self.confidence is not None:
            data["confidence"] = round(float(self.confidence), 2)
        if self.session_id:
            data["session_id"] = self.session_id
        if self.project:
            data["project"] = self.project
        if self.retrieved_at:
            data["retrieved_at"] = self.retrieved_at
        if self.valid_until:
            data["valid_until"] = self.valid_until
        return data

    def to_markdown(self) -> str:
        header = yaml.safe_dump(
            self.frontmatter(),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        body = self.body.strip("\n")
        return f"---\n{header}\n---\n\n{body}\n"

    @classmethod
    def from_markdown(cls, text: str) -> "KnowledgeNote":
        match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
        if not match:
            raise ValueError("note is missing a frontmatter block")
        meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            raise ValueError("note frontmatter is not a mapping")
        return cls(
            id=str(meta.get("id") or ""),
            type=str(meta.get("type") or "note"),
            title=str(meta.get("title") or ""),
            body=match.group(2).strip("\n"),
            tags=[str(t) for t in (meta.get("tags") or [])],
            sources=[str(s) for s in (meta.get("sources") or [])],
            aliases=[str(a) for a in (meta.get("aliases") or [])],
            relations=[r for r in (meta.get("relations") or []) if isinstance(r, dict)],
            open_questions=clean_open_questions(meta.get("open_questions") or []),
            confidence=_as_float(meta.get("confidence")),
            status=str(meta.get("status") or "active"),
            session_id=str(meta.get("session_id") or ""),
            project=str(meta.get("project") or ""),
            retrieved_at=_as_str(meta.get("retrieved_at")),
            valid_until=_as_str(meta.get("valid_until")),
            created=str(meta.get("created") or ""),
            updated=str(meta.get("updated") or ""),
        )


def wikilink(target: str) -> str:
    return f"[[{target}]]"


def clean_open_questions(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        text = text[:MAX_OPEN_QUESTION_CHARS].strip()
        if _is_safe_open_question(text) and text not in out:
            out.append(text)
        if len(out) >= MAX_OPEN_QUESTIONS:
            break
    return out


def _is_safe_open_question(text: str) -> bool:
    return is_prompt_visible_text_safe(text)


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_str(value) -> str | None:
    return str(value) if value not in (None, "") else None
