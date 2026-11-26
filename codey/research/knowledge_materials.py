"""Read pinned knowledge material through the ordinary source receipt channel."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from codey.knowledge.model import KnowledgeVersion
from codey.knowledge.recall import KnowledgeAccess
from codey.research.passages import PassageDescriptor, describe_captured_window, passage_ref, restore_passages
from codey.research.source_document import OpenedSource, SourceQuality
from codey.research.source_observation import source_snapshot
from codey.types.json_value import require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.research.input_tools import ResearchInputTools
    from codey.research.tool_output import ResearchToolOutput


def _source_quality(value: object) -> SourceQuality:
    if not isinstance(value, dict):
        raise ValueError("source quality must be an object")
    quality: Mapping[object, object] = value
    return SourceQuality(**{require_json_string(key, "quality key"): require_json_string(item, "source quality")
                            for key, item in quality.items()})


def _page_texts(value: object) -> dict[int, str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("stored pages must be an array")
    pages: Sequence[object] = value
    result: dict[int, str] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("stored page must be an object")
        row: Mapping[str, object] = page
        number = require_json_integer(row["number"], "page number")
        result[number] = require_json_string(row["text"], "page text")
    return result


def read_material(tools: ResearchInputTools, version: KnowledgeVersion, *, offset: int = 0, limit: int = 6000) -> ResearchToolOutput:
    """No acquisition or writes. Every displayed byte belongs to an exact version."""
    from codey.research.tool_output import ResearchToolOutput

    if tools.store is None:
        raise ValueError("knowledge material requires a store")

    value = version.value
    evidence = version if value.kind == "evidence" else None
    if evidence is not None:
        refs = [ref for ref in value.references if ref.relation == "from_source"]
        if len(refs) != 1:
            raise ValueError("stored evidence requires one pinned source")
        source_version = tools.store.repository.read(refs[0].target, refs[0].revision)
    else:
        source_version = version
    access = KnowledgeAccess(tools.project, tools.session_id, tools.store.owner_id)
    if source_version is None or source_version.value.kind != "source" or not access.allows(source_version):
        raise ValueError("stored material source is missing or inaccessible")
    version = source_version
    source_value = version.value
    text, provenance = source_value.content, source_value.provenance
    url = require_json_string(provenance["url"], "source URL")
    source = OpenedSource(
        requested_url=url, final_url=url, title=source_value.title,
        retrieved_at=require_json_string(provenance.get("retrieved_at") or version.recorded_at, "retrieved_at"),
        text_hash=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16],
        quality=_source_quality(provenance.get("quality", {})),
        content_kind=require_json_string(provenance.get("content_kind", "html"), "content kind"),
        truncated=bool(provenance.get("truncated", False)))
    pages = provenance.get("pages", [])
    rows: list[PassageDescriptor]
    if evidence is not None:
        start = evidence.value.provenance.get("start")
        if type(start) is not int:
            start = text.find(evidence.value.content)
        end = start + len(evidence.value.content)
        if start < 0 or text[start:end] != evidence.value.content:
            raise ValueError("stored evidence differs from its pinned source")
        if end - start > 12000:
            raise ValueError("stored evidence exceeds read budget; read its pinned source using offset/limit")
        rows = [{"ref": passage_ref(source.final_url, source.text_hash, start, end), "start": start, "end": end,
                 "evidence_version": (evidence.value.id, evidence.revision)}]
        if not pages and evidence.value.provenance.get("page"):
            pages = [{"number": evidence.value.provenance["page"], "text": evidence.value.content}]
    else:
        if type(offset) is not int or type(limit) is not int or not 0 <= offset < len(text) or not 500 <= limit <= 12000:
            raise ValueError("source read needs offset within the stored text and limit between 500 and 12000")
        rows = []
    observation = source_snapshot(source, text, _page_texts(pages))
    if evidence is None:
        rows = describe_captured_window(observation, offset, limit, coherent=True)
    if not rows:
        raise ValueError("stored source window contains no readable text")
    for row in rows:
        row["source_version"] = (source_value.id, version.revision)
        row["ref"] = passage_ref(source.final_url, source.text_hash, row["start"], row["end"],
            source_version=row["source_version"], evidence_version=row.get("evidence_version"))
    restore_passages(tools.ledger, observation, rows)
    body = {"id": value.id, "revision": evidence.revision if evidence else version.revision,
            "kind": value.kind, "source_version": [source_value.id, version.revision],
            "available_length": len(text), "next_offset": rows[-1]["end"],
            "references": [ref.to_dict() for ref in value.references],
            "passages": [{**row, "text": text[row["start"]:row["end"]]} for row in rows]}
    from codey.knowledge.model import canonical_json

    return ResearchToolOutput(canonical_json(body), read_passages=tuple(rows),
                              read_observation=observation, ok=True)
