"""Compile only explicitly cited findings into source and knowledge references."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from codey.research.ledger import ResearchLedger
from codey.research.questions import validate_question_coverage
from codey.reviews.report_sections import render_done_report, section_title

FINDING_CITATION = re.compile(r"\[(f[1-9][0-9]*)\]")


@dataclass(frozen=True)
class FindingReport:
    text: str
    knowledge_refs: tuple[dict[str, Any], ...]
    finding_ids: tuple[str, ...]
    validation_ledger: ResearchLedger | None = None
    inference_statements: tuple[str, ...] = ()
    reference_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


def compile_finding_report(args: dict[str, Any], ledger: ResearchLedger, store: Any, *,
                           project: str = "", session_id: str = "") -> FindingReport:
    sections = args.get("report_sections")
    raw = render_done_report(str(args.get("summary") or ""), sections, questions=ledger.questions)
    assert isinstance(sections, dict)
    validate_question_coverage(sections["coverage"], ledger)
    selected = list(dict.fromkeys(FINDING_CITATION.findall(raw)))
    if re.search(r"\[(?:\d+|s\d+)(?:\s*[,;][^\]]*)?\]", raw):
        raise ValueError("Replace numeric/source citations with exact [f...] finding references. "
                         "The application assigns source numbers.")
    dangling = re.findall(r"\[f[^\]]*\]", FINDING_CITATION.sub("", raw))
    if dangling:
        raise ValueError("Unknown finding citation: " + ", ".join(dangling[:4]) + "; use only these exact references: "
                         + ", ".join(f"[{key}]" for key in ledger.finding_refs))
    if not selected:
        raise ValueError("Cite saved findings as [f1], [f2], etc. in summary and evidence. "
                         "Available references: " + ", ".join(f"[{key}]" for key in ledger.finding_refs))
    number_by_url: dict[str, int] = {}
    number_by_finding = {}
    sources = []
    references = []
    identities = []
    inferred = []
    metadata = {}
    adopted = [label for label in selected if label in ledger.knowledge_refs]
    validation_ledger = None
    if adopted:
        from codey.research.references import evidence_view

        validation_ledger, versions, adopted_urls = evidence_view(ledger, store, adopted,
                                                                  project=project, session_id=session_id)
    for label in selected:
        if label in ledger.knowledge_refs:
            version = versions[label]
            references.append({"id": version.value.id, "revision": version.revision})
            metadata[label] = {"id": version.value.id, "revision": version.revision,
                "kind": version.value.kind, "status": version.value.status,
                "interpretation_review": version.interpretation_review,
                "dependencies_changed": version.dependencies_changed, "conditions": version.value.conditions,
                "limitations": version.value.provenance.get("limitations", ""), "origin": version.value.origin}
            if version.value.kind == "inference":
                inferred.append(version.value.content)
            numbers = []
            for url in adopted_urls[label]:
                if url not in number_by_url:
                    number_by_url[url] = len(number_by_url) + 1
                    assert validation_ledger is not None
                    source = validation_ledger.source_record_for_url(url)
                    assert source is not None
                    sources.append(f"[{number_by_url[url]}] {source.title}（已保存的精确来源版本，本轮未重新获取） - {url}")
                numbers.append(number_by_url[url])
            number_by_finding[label] = "".join(f"[{number}]" for number in numbers)
            if version.value.kind == "inference":
                number_by_finding[label] = f"（候选推论 {label}，未审查；所列来源为前提依据）" + number_by_finding[label]
            continue
        finding = ledger.finding_refs.get(label, "")
        if not finding:
            raise ValueError(f"Unknown finding [{label}]; available: " + ", ".join(ledger.finding_refs))
        identities.append(finding)
        passage, claim_refs = _current_finding(finding, ledger, store)
        references.extend(claim_refs)
        url = passage.source.final_url
        if url not in number_by_url:
            number_by_url[url] = len(number_by_url) + 1
            sources.append(f"[{number_by_url[url]}] {passage.source.title} - {url}")
        number_by_finding[label] = f"[{number_by_url[url]}]"
    compiled = FINDING_CITATION.sub(lambda match: number_by_finding[match[1]], raw)
    if metadata:
        compiled += "\n\n已有知识按读取的精确版本引用，其原始来源本轮未重新获取；解释仍未经独立审查。"
        for label, row in metadata.items():
            if row["dependencies_changed"]:
                compiled += f"\n{label} 的前提已有变化，需重新检查适用性。"
            if row["status"] in {"withdrawn", "stale", "superseded", "contradicted"}:
                compiled += f"\n{label} 当前记录状态为 {row['status']}，须按其限制解读。"
            if row["limitations"]:
                compiled += f"\n候选推论 {label} 的限制：{row['limitations']}"
    compiled += f"\n\n## {section_title('sources')}\n" + "\n".join(sources)
    if ledger.searches:
        queries = "\n".join("- " + item.query for item in ledger.searches[:12])
        marker = f"## {section_title('coverage')}\n"
        compiled = compiled.replace(marker, marker + "实际检索词（最多显示 12 条）：\n" + queries + "\n\n", 1)
    return FindingReport(compiled, tuple(references), tuple(identities), validation_ledger, tuple(inferred), metadata)


def _current_finding(finding: str, ledger: ResearchLedger, store: Any) -> tuple[Any, list[dict[str, Any]]]:
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
    return passage, claim_refs
