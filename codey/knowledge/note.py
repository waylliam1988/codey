"""Knowledge document values and their Markdown interchange format.

The user-facing feature is Research, but the internal store is broader than
research reports: decisions, implementation receipts, and verification evidence
all become versioned SQLite documents with explicit provenance. Markdown is
an explicit import/export format.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Self, SupportsFloat, SupportsIndex, TypedDict, Unpack

from codey.knowledge.concept_schema import ConceptRelation, clean_relations
from codey.policies.prompt_safety import is_prompt_visible_text_safe
from codey.types.json_value import JsonObject, decode_json_object, require_json_string, require_json_strings

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
_WIKILINK_KIND_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\](?:\s*\(([a-z_]+)\))?")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff._-]*$")


def now_iso() -> str:
    return datetime.now(UTC).astimezone().replace(microsecond=0).isoformat()


def slugify(title: str, limit: int = 48) -> str:
    text = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    return (text[:limit].strip("-")) or "note"


def make_id(title: str) -> str:
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{slugify(title)}-{uuid.uuid4().hex[:8]}"


def is_safe_id(note_id: str) -> bool:
    note_id = (note_id or "").strip()
    if not note_id or len(note_id) > 200:
        return False
    if ".." in note_id or "/" in note_id or "\\" in note_id:
        return False
    return bool(_SAFE_ID_RE.match(note_id))


class _NoteOptions(TypedDict, total=False):
    tags: list[str]
    sources: list[str]
    aliases: list[str]
    relations: list[ConceptRelation]
    open_questions: list[str]
    confidence: float | None
    status: str
    session_id: str
    project: str
    retrieved_at: str | None
    valid_until: str | None
    created: str
    updated: str


@dataclass
class KnowledgeNote:
    id: str
    type: str = "note"
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    relations: list[ConceptRelation] = field(default_factory=list)
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
    def create(cls, *, type: str, title: str, body: str, id: str | None = None, **kwargs: Unpack[_NoteOptions]) -> Self:
        return cls(id=id or make_id(title), type=type, title=title, body=body, **kwargs)

    @classmethod
    def from_payload(cls, value: object) -> KnowledgeNote:
        """Decode a frozen document body with its exact stored field values."""
        raw = decode_json_object(value)
        if set(raw) != {item.name for item in fields(cls)}:
            raise ValueError("invalid knowledge document payload")
        relations, dropped = clean_relations(raw["relations"])
        confidence = raw["confidence"]
        if dropped or (confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (float, int)))):
            raise ValueError("invalid knowledge document relations or confidence")
        retrieved_at = raw["retrieved_at"]
        valid_until = raw["valid_until"]
        note = cls(
            id=require_json_string(raw["id"], "id"),
            type=require_json_string(raw["type"], "type"),
            title=require_json_string(raw["title"], "title"),
            body=require_json_string(raw["body"], "body"),
            tags=require_json_strings(raw["tags"], "tags"),
            sources=require_json_strings(raw["sources"], "sources"),
            aliases=require_json_strings(raw["aliases"], "aliases"),
            relations=relations,
            open_questions=require_json_strings(raw["open_questions"], "open_questions"),
            confidence=confidence,
            status=require_json_string(raw["status"], "status"),
            session_id=require_json_string(raw["session_id"], "session_id"),
            project=require_json_string(raw["project"], "project"),
            retrieved_at=require_json_string(retrieved_at, "retrieved_at") if retrieved_at is not None else None,
            valid_until=require_json_string(valid_until, "valid_until") if valid_until is not None else None,
            created=require_json_string(raw["created"], "created"),
            updated=require_json_string(raw["updated"], "updated"),
        )
        if asdict(note) != raw:
            raise ValueError("knowledge document differs from frozen payload")
        return note

    @property
    def folder(self) -> str:
        return _TYPE_FOLDERS.get(self.type, "notes")

    def wikilink_edges(self) -> list[tuple[str, str]]:
        edges: dict[str, str] = {}
        for match in _WIKILINK_KIND_RE.finditer(self.body):
            target, relation = match.groups("")
            target = target.strip()
            if not target:
                continue
            kind = relation if relation in LINK_KINDS else "relates"
            if target not in edges or (edges[target] == "relates" and kind != "relates"):
                edges[target] = kind
        return list(edges.items())

    def frontmatter(self) -> JsonObject:
        data: JsonObject = {
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
            data["relations"] = [{"src": relation["src"], "dst": relation["dst"], "kind": relation["kind"]}
                                 for relation in self.relations]
        if self.open_questions:
            data["open_questions"] = list(self.open_questions)
        if self.confidence is not None:
            confidence = _safe_confidence(self.confidence)
            if confidence is not None:
                data["confidence"] = confidence
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
        # Keep PyYAML out of the normal application import path.
        from yaml import safe_dump

        header = safe_dump(
            self.frontmatter(),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        body = self.body.strip("\n")
        return f"---\n{header}\n---\n\n{body}\n"

    @classmethod
    def from_markdown(cls, text: str) -> KnowledgeNote:
        from yaml import YAMLError, safe_load

        match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
        if not match:
            raise ValueError("note is missing a frontmatter block")
        header, body = match.groups("")
        try:
            loaded: object = safe_load(header)
        except YAMLError as exc:
            raise ValueError("invalid note frontmatter syntax") from exc
        if not loaded:
            meta: Mapping[object, object] = {}
        elif isinstance(loaded, dict):
            meta = loaded
        else:
            raise ValueError("note frontmatter is not a mapping")
        return cls(
            id=str(meta.get("id") or ""),
            type=str(meta.get("type") or "note"),
            title=str(meta.get("title") or ""),
            body=body.strip("\n"),
            tags=_metadata_strings(meta.get("tags")),
            sources=_metadata_strings(meta.get("sources")),
            aliases=_metadata_strings(meta.get("aliases")),
            relations=clean_relations(meta.get("relations"))[0],
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


def _metadata_strings(value: object) -> list[str]:
    if not value:
        return []
    if not isinstance(value, Iterable):
        raise ValueError("note frontmatter list field is not iterable")
    items: Iterable[object] = value
    return [str(item) for item in items]


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, bytearray, SupportsFloat, SupportsIndex)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _safe_confidence(value: object) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    return round(max(0.0, min(1.0, number)), 2)


def _as_str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None
