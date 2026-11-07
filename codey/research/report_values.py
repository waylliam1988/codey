"""Named report wire values shared by compilers and their strict codecs."""

from typing import NotRequired, TypedDict

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
from codey.research.knowledge_update_types import MutationPayload
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    canonical_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


class VersionReference(TypedDict):
    id: str
    revision: int


class SourceVersion(VersionReference):
    title: str
    url: str
    content_hash: str
    retrieved_at: str
    quality: dict[str, str]
    number: NotRequired[int]
    acquisition: NotRequired[str]
    publication_date: NotRequired[str]
    release_version: NotRequired[str]


class EvidenceBinding(TypedDict):
    label: str
    evidence_id: str
    evidence_revision: int
    source_id: str
    source_revision: int
    excerpt: str
    excerpt_digest: str
    page: int | None
    locator: str
    relation: str
    claim_id: NotRequired[str]
    claim_revision: NotRequired[int]
    acquisition: NotRequired[str]
    display_truncated: NotRequired[bool]
    version_mentions: NotRequired[JsonValue]
    passage_ref: NotRequired[str]
    start: NotRequired[int]
    end: NotRequired[int]


class CitationBinding(TypedDict):
    label: str
    start: int
    end: int
    passage_ref: str


class ParagraphBinding(TypedDict):
    path: str
    text: str
    refs: list[str]
    kinds: list[str]


class FindingMetadata(VersionReference):
    claim: str
    question_ref: str
    kind: str
    status: str
    interpretation_review: str
    dependencies_changed: bool
    conditions: dict[str, dict[str, str]]
    origin: str
    acquisition: str
    limitations: JsonValue
    version_mentions: NotRequired[JsonValue]


def object_rows(value: object, field: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    return tuple(canonical_json_object(row) for row in value)


def strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array of strings")
    return tuple(require_json_string(item, field) for item in value)


def version_reference(value: object) -> VersionReference:
    row = canonical_json_object(value)
    identity = require_json_string(row.get("id"), "id")
    revision = require_json_integer(row.get("revision"), "revision")
    KnowledgeReference(identity, revision, "uses")
    return VersionReference(id=identity, revision=revision)


def mutation_payload(value: object) -> MutationPayload:
    row = canonical_json_object(value)
    if set(row) != {"value", "expected_revision"}:
        raise ValueError("invalid knowledge mutation fields")
    raw_value = canonical_json_object(row["value"])
    expected = require_json_integer(row["expected_revision"], "expected_revision")
    KnowledgeMutation(KnowledgeObject.from_dict(raw_value), expected)
    return MutationPayload(value=raw_value, expected_revision=expected)


def source_version(value: object) -> SourceVersion:
    row = canonical_json_object(value)
    ref = version_reference(row)
    quality = row.get("quality")
    if not isinstance(quality, dict):
        raise ValueError("source quality must be an object")
    result = SourceVersion(id=ref["id"], revision=ref["revision"],
        title=require_json_string(row.get("title"), "title"),
        url=require_json_string(row.get("url"), "url"),
        content_hash=require_json_string(row.get("content_hash"), "content_hash"),
        retrieved_at=require_json_string(row.get("retrieved_at"), "retrieved_at"),
        quality={key: require_json_string(item, key) for key, item in quality.items()})
    if "number" in row:
        result["number"] = require_json_integer(row["number"], "number")
    if "acquisition" in row:
        result["acquisition"] = require_json_string(row["acquisition"], "acquisition")
    if "publication_date" in row:
        result["publication_date"] = require_json_string(row["publication_date"], "publication_date")
    if "release_version" in row:
        result["release_version"] = require_json_string(row["release_version"], "release_version")
    return result


def evidence_binding(value: object) -> EvidenceBinding:
    row = canonical_json_object(value)
    page = row.get("page")
    result = EvidenceBinding(
        label=require_json_string(row.get("label"), "label"),
        evidence_id=require_json_string(row.get("evidence_id"), "evidence_id"),
        evidence_revision=require_json_integer(row.get("evidence_revision"), "evidence_revision"),
        source_id=require_json_string(row.get("source_id"), "source_id"),
        source_revision=require_json_integer(row.get("source_revision"), "source_revision"),
        excerpt=require_json_string(row.get("excerpt"), "excerpt"),
        excerpt_digest=require_json_string(row.get("excerpt_digest"), "excerpt_digest"),
        page=None if page is None else require_json_integer(page, "page"),
        locator=require_json_string(row.get("locator"), "locator"),
        relation=require_json_string(row.get("relation"), "relation"))
    if "claim_id" in row:
        result["claim_id"] = require_json_string(row["claim_id"], "claim_id")
    if "claim_revision" in row:
        result["claim_revision"] = require_json_integer(row["claim_revision"], "claim_revision")
    if "acquisition" in row:
        result["acquisition"] = require_json_string(row["acquisition"], "acquisition")
    if "display_truncated" in row:
        result["display_truncated"] = require_json_boolean(row["display_truncated"], "display_truncated")
    if "version_mentions" in row:
        result["version_mentions"] = row["version_mentions"]
    if "passage_ref" in row:
        result["passage_ref"] = require_json_string(row["passage_ref"], "passage_ref")
    if "start" in row:
        result["start"] = require_json_integer(row["start"], "start")
    if "end" in row:
        result["end"] = require_json_integer(row["end"], "end")
    KnowledgeReference(result["evidence_id"], result["evidence_revision"], "uses")
    KnowledgeReference(result["source_id"], result["source_revision"], "uses")
    return result


def citation_binding(value: object) -> CitationBinding:
    row = canonical_json_object(value)
    start = require_json_integer(row.get("start"), "start")
    end = require_json_integer(row.get("end"), "end")
    if not 0 <= start < end:
        raise ValueError("invalid citation range")
    return CitationBinding(label=require_json_string(row.get("label"), "label"), start=start, end=end,
                           passage_ref=require_json_string(row.get("passage_ref"), "passage_ref"))


def paragraph_binding(value: object) -> ParagraphBinding:
    row = canonical_json_object(value)
    return ParagraphBinding(path=require_json_string(row.get("path"), "path"),
        text=require_json_string(row.get("text"), "text"), refs=list(strings(row.get("refs"), "refs")),
        kinds=list(strings(row.get("kinds"), "kinds")))


def finding_metadata(value: object) -> FindingMetadata:
    from codey.research.tool_args import decode_conditions

    row = canonical_json_object(value)
    ref = version_reference(row)
    result = FindingMetadata(id=ref["id"], revision=ref["revision"],
        claim=require_json_string(row.get("claim"), "claim"),
        question_ref=require_json_string(row.get("question_ref"), "question_ref"),
        kind=require_json_string(row.get("kind"), "kind"),
        status=require_json_string(row.get("status"), "status"),
        interpretation_review=require_json_string(row.get("interpretation_review"), "interpretation_review"),
        dependencies_changed=require_json_boolean(row.get("dependencies_changed"), "dependencies_changed"),
        conditions=decode_conditions(row.get("conditions")) or {},
        origin=require_json_string(row.get("origin"), "origin"),
        acquisition=require_json_string(row.get("acquisition"), "acquisition"),
        limitations=row.get("limitations", ""))
    if "version_mentions" in row:
        result["version_mentions"] = row["version_mentions"]
    return result
