"""Validate a structured report, resolve exact versions, then render once."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from codey.knowledge.model import KnowledgeReference, canonical_json
from codey.research.evidence_view import ResolvedReference, resolve_selected_references
from codey.research.issues import ResearchIssue
from codey.research.ledger import ResearchLedger
from codey.research.questions import validate_question_coverage
from codey.research.report_proposal import FINDING_CITATION, ResearchReportProposal, parse_report_proposal
from codey.reviews.report_sections import section_title
from codey.reviews.research_coverage import coverage_rows, render_coverage


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class FindingReport:
    text: str
    knowledge_refs: tuple[dict[str, Any], ...]
    finding_ids: tuple[str, ...]
    coverage: tuple[dict[str, Any], ...]
    reference_metadata: dict[str, dict[str, Any]]
    source_versions: tuple[dict[str, Any], ...]
    paragraph_bindings: tuple[dict[str, Any], ...]
    proposal_digest: str
    questions: tuple[str, ...]
    evidence_bindings: tuple[dict[str, Any], ...] = ()
    open_questions: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        # Normalize tuples at the persistence boundary, then bind every field.
        import json

        payload = json.loads(canonical_json(payload))
        return {**payload, "compilation_digest": _digest(payload)}

    @classmethod
    def from_payload(cls, raw: Any) -> FindingReport:
        fields = {"text", "knowledge_refs", "finding_ids", "coverage", "reference_metadata",
                  "source_versions", "paragraph_bindings", "proposal_digest", "questions", "evidence_bindings", "open_questions"}
        if not isinstance(raw, dict) or set(raw) != fields | {"compilation_digest"}:
            raise ValueError("invalid accepted research report")
        body = {key: raw[key] for key in fields}
        if raw["compilation_digest"] != _digest(body):
            raise ValueError("accepted research report digest mismatch")
        if not isinstance(body["text"], str) or not body["text"].strip():
            raise ValueError("accepted research report has no text")
        for row in body["knowledge_refs"]:
            KnowledgeReference(row["id"], row["revision"], "uses")
        coverage_rows(body["coverage"])
        return cls(body["text"], tuple(body["knowledge_refs"]), tuple(body["finding_ids"]),
            tuple(body["coverage"]), body["reference_metadata"], tuple(body["source_versions"]),
            tuple(body["paragraph_bindings"]), body["proposal_digest"], tuple(body["questions"]), tuple(body["evidence_bindings"]),
            tuple(body["open_questions"]))


def validate_report_bindings(proposal: ResearchReportProposal, resolved: tuple[ResolvedReference, ...]) -> None:
    by_label = {item.label: item for item in resolved}
    for answer, coverage in zip(proposal.coverage_answers, proposal.coverage, strict=True):
        if not set(answer.refs) <= set(coverage["findings"]):
            raise ResearchIssue("coverage_reference_mismatch", answer.path,
                "answer citations must also occur in this question's findings", allowed_refs=tuple(coverage["findings"]))
    for row in (proposal.conclusion, *proposal.evidence):
        if not row.refs:
            raise ResearchIssue("missing_citation", row.path, "cite the exact finding adopted in this paragraph",
                                allowed_refs=tuple(by_label))
    # An uncited counter paragraph may state a recorded gap; it is not contrary
    # evidence. Whether its prose accurately describes that gap remains unreviewed.
    if all(row["state"] == "evidence_found" for row in proposal.coverage):
        for row in proposal.counter:
            if not row.refs:
                raise ResearchIssue("counter_basis_missing", row.path,
                    "cite a finding for observed limitations, or record the specific unresolved question in coverage")


def render_report(proposal: ResearchReportProposal, resolved: tuple[ResolvedReference, ...],
                  questions: tuple[str, ...], queries: tuple[str, ...]) -> tuple[str, tuple[dict[str, Any], ...]]:
    sources: dict[tuple[str, int], dict[str, Any]] = {}
    replacements = {}
    for item in resolved:
        numbers = []
        for evidence in item.evidence:
            source = evidence.source
            key = source.value.id, source.revision
            if key not in sources:
                sources[key] = {"id": key[0], "revision": key[1], "number": len(sources) + 1,
                    "title": source.value.title, "url": source.value.provenance["url"],
                    "acquisition": evidence.acquisition,
                    "content_hash": source.value.provenance.get("content_hash", ""),
                    "quality": source.value.provenance.get("quality", {}),
                    "retrieved_at": source.value.provenance.get("retrieved_at", source.recorded_at)}
            if sources[key]["number"] not in numbers:
                numbers.append(sources[key]["number"])
        citations = "".join(f"[{number}]" for number in numbers)
        if item.version.value.kind == "inference":
            citations = f"（候选推论 {item.label}，未审查；来源为前提依据）" + citations
        replacements[item.label] = citations

    def replace(text: str) -> str:
        return FINDING_CITATION.sub(lambda match: replacements[match[1]], text)

    sections = {"conclusion": replace(proposal.conclusion.text),
        **{key: "\n\n".join(replace(row.text) for row in getattr(proposal, key))
           for key in ("evidence", "counter", "source_quality")},
        "coverage": render_coverage([{**row, "answer": replace(row["answer"])} for row in proposal.coverage],
                                    questions, render_ref=lambda label: replacements[label])}
    if queries:
        sections["coverage"] = ("实际检索词（最多显示 12 条）：\n" + "\n".join("- " + q for q in queries[:12])
                                + "\n\n" + sections["coverage"])
    notes = ["所采用的主张与推论仍未经独立语义审查；保存、引用有效不代表解释正确。"]
    for item in resolved:
        value = item.version.value
        if item.binding.acquisition == "prior_knowledge":
            notes.append(f"{item.label} 使用已有知识的精确版本，原始来源本轮未重新获取。")
        if item.version.dependencies_changed:
            notes.append(f"{item.label} 的前提已有变化，需重新检查适用性。")
        if value.status in {"withdrawn", "stale", "superseded", "contradicted"}:
            notes.append(f"{item.label} 当前记录状态为 {value.status}，须按其限制解读。")
        if value.kind == "inference" and value.provenance.get("limitations"):
            notes.append(f"候选推论 {item.label} 的限制：{value.provenance['limitations']}")
    sections["source_quality"] += "\n\n" + "\n".join(notes)
    source_lines = []
    for row in sources.values():
        origin = "（已保存的精确来源版本，本轮未重新获取）" if row["acquisition"] == "prior_knowledge" else ""
        source_lines.append(f"[{row['number']}] {row['title']}{origin} - {row['url']}")
    sections["sources"] = "\n".join(source_lines)
    return "\n\n".join(f"## {section_title(key)}\n{text}" for key, text in sections.items()), tuple(sources.values())


def compile_finding_report(args: dict[str, Any], ledger: ResearchLedger, store: Any, *,
                           project: str = "", session_id: str = "") -> FindingReport:
    proposal = parse_report_proposal(args)
    validate_question_coverage(list(proposal.coverage), ledger)
    for row in proposal.paragraphs:
        for label in row.refs:
            if label not in ledger.references:
                raise ResearchIssue("unknown_reference", row.path, f"unknown finding [{label}]",
                                    allowed_refs=tuple(ledger.references))
    if not proposal.selected_refs:
        raise ResearchIssue("missing_evidence", "summary", "no saved or read finding is selected",
                            requires_acquisition=True)
    resolved = resolve_selected_references(proposal.selected_refs, ledger, store, project=project, session_id=session_id)
    validate_report_bindings(proposal, resolved)
    text, sources = render_report(proposal, resolved, ledger.questions, tuple(row.query for row in ledger.searches))
    metadata = {item.label: {"id": item.version.value.id, "revision": item.version.revision,
        "kind": item.version.value.kind, "status": item.version.value.status,
        "interpretation_review": item.version.interpretation_review,
        "dependencies_changed": item.version.dependencies_changed, "conditions": item.version.value.conditions,
        "origin": item.version.value.origin, "acquisition": item.binding.acquisition,
        "limitations": item.version.value.provenance.get("limitations", "")}
        for item in resolved}
    paragraphs = tuple({"path": row.path, "text": row.text, "refs": list(row.refs),
        "kinds": [metadata[label]["kind"] for label in row.refs]} for row in proposal.paragraphs)
    evidence_bindings = tuple({"label": item.label, "claim_id": ev.claim.value.id,
        "claim_revision": ev.claim.revision, "evidence_id": ev.evidence.value.id,
        "evidence_revision": ev.evidence.revision, "source_id": ev.source.value.id,
        "source_revision": ev.source.revision, "excerpt": ev.evidence.value.content[:600],
        "excerpt_digest": _digest(ev.evidence.value.content), "display_truncated": len(ev.evidence.value.content) > 600,
        "relation": ev.relation, "locator": ev.evidence.value.provenance.get("locator", ""),
        "page": ev.evidence.value.provenance.get("page"), "acquisition": ev.acquisition}
        for item in resolved for ev in item.evidence)
    return FindingReport(text, tuple({"id": item.version.value.id, "revision": item.version.revision} for item in resolved),
        tuple(dict.fromkeys(item.binding.note_id for item in resolved if item.binding.note_id)),
        proposal.coverage, metadata, sources, paragraphs, _digest(args), ledger.questions, evidence_bindings, tuple(args.get("open_questions", ())))
