"""Compile only explicitly cited findings into source and knowledge references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from codey.research.ledger import ResearchLedger
from codey.reviews.report_sections import render_done_report, section_title

FINDING_CITATION = re.compile(r"\[(f[0-9a-f]{16})\]")


@dataclass(frozen=True)
class FindingReport:
    text: str
    knowledge_refs: tuple[dict[str, Any], ...]
    finding_ids: tuple[str, ...]


def compile_finding_report(args: dict[str, Any], ledger: ResearchLedger, store: Any) -> FindingReport:
    raw = render_done_report(str(args.get("summary") or ""), args.get("report_sections"))
    selected = list(dict.fromkeys(FINDING_CITATION.findall(raw)))
    if not selected:
        raise ValueError("Cite saved findings as [f...] in summary and evidence; copy finding_ref from save_finding. "
                         "Do not invent numeric citations or a source table.")
    if re.search(r"\[(?:\d+|s\d+)(?:\s*[,;][^\]]*)?\]", raw):
        raise ValueError("Replace numeric/source citations with exact [f...] finding references. "
                         "The application assigns source numbers.")
    dangling = re.findall(r"\[f[^\]]*\]", FINDING_CITATION.sub("", raw))
    if dangling:
        raise ValueError("Unknown finding citation: " + ", ".join(dangling[:4]) + "; copy an exact finding_ref")
    number_by_url: dict[str, int] = {}
    number_by_finding = {}
    sources = []
    references = []
    for finding in selected:
        items = list({item for item in ledger.evidence_items if item.note_id == finding
                      and item.locator.startswith("passage:")})
        if len(items) != 1:
            raise ValueError(f"Unknown or ambiguous finding [{finding}]; select a successful save_finding result")
        item = items[0]
        passage = ledger.read_passages.get(item.locator.removeprefix("passage:"))
        if passage is None or passage.excerpt.strip() != item.excerpt or passage.source.final_url != item.source_url:
            raise ValueError(f"Finding [{finding}] has no matching frozen source passage")
        if store is None:
            raise ValueError("Cannot resolve finding knowledge versions: store unavailable")
        rows = store.note_references(finding)
        claim_refs = []
        for row in rows:
            version = store.repository.read(str(row["target"]), int(row["revision"]))
            if version is not None and version.value.kind == "claim":
                if version.value.content != item.claim or version.value.provenance.get("note_id") != finding:
                    raise ValueError(f"Finding [{finding}] has conflicting knowledge content")
                claim_refs.append({"id": version.value.id, "revision": version.revision})
        if len(claim_refs) != 1:
            raise ValueError(f"Finding [{finding}] lacks one pinned claim version")
        references.extend(claim_refs)
        url = passage.source.final_url
        if url not in number_by_url:
            number_by_url[url] = len(number_by_url) + 1
            sources.append(f"[{number_by_url[url]}] {passage.source.title} - {url}")
        number_by_finding[finding] = number_by_url[url]
    compiled = FINDING_CITATION.sub(lambda match: f"[{number_by_finding[match[1]]}]", raw)
    compiled += f"\n\n## {section_title('sources')}\n" + "\n".join(sources)
    if ledger.searches:
        queries = "\n".join("- " + item.query for item in ledger.searches[:12])
        marker = f"## {section_title('coverage')}\n"
        compiled = compiled.replace(marker, marker + "实际检索词（最多显示 12 条）：\n" + queries + "\n\n", 1)
    return FindingReport(compiled, tuple(references), tuple(selected))
