"""ResearchRecord delivery projection from the accepted compilation only."""

from typing import Any

from codey.research.finding_report import FindingReport
from codey.research.identity import project_ref, sanitize_research_url_ref
from codey.research.object_model import (
    EvidenceLocator,
    ResearchAssumption,
    ResearchClaim,
    ResearchEvidence,
    ResearchQuestion,
    ResearchRecord,
    ResearchSource,
)
from codey.utils.refs import digest_json, digest_text, stable_ref


def validate_accepted_projection(result: Any, *, run_id: str, session_id: str, project: str) -> None:
    compiled = result.accepted_report
    # Validate the frozen payload before any downstream queue decision.
    FindingReport.from_payload(compiled.to_payload())
    assessment = result.semantic_review
    if (result.summary != compiled.text or result.stop_reason != "done" or not isinstance(assessment, dict)
            or assessment.get("status") != "passed" or assessment.get("issues")
            or assessment.get("kind") != "same_provider_model_assessment"
            or assessment.get("report_digest") != compiled.to_payload()["compilation_digest"]
            or not assessment.get("result_ref")):
        raise ValueError("accepted report lacks its matching completed model assessment")
    from codey.knowledge.model import canonical_json
    from codey.research.semantic_review import parse_review, review_issues

    judgments = parse_review(canonical_json(assessment["assessment"]), {
        "task": result.question, "questions": compiled.questions,
        "claims": [{"ref": ref} for ref in compiled.reference_metadata], "paragraphs": compiled.paragraph_bindings})
    proof = result.completion_proof
    required = {"task_coverage_assessed", "claim_support_assessed", "report_scope_assessed"}
    if (review_issues(judgments) or proof is None or not proof.satisfied
            or not required <= {row.check_id for row in proof.checks
                                if row.status == "pass" and row.reason_code == "same_provider_model_assessment"}
            or assessment.get("assessment_effect_id") not in proof.evidence_refs
            or assessment["result_ref"] not in proof.artifact_refs
            or not result.synthesis_id or result.synthesis_id != assessment.get("synthesis_id")):
        raise ValueError("accepted report differs from its assessed completion proof")
    expected = project_accepted_report(compiled, question=result.question, run_id=run_id, session_id=session_id,
                                       project=project, synthesis_id=result.synthesis_id)
    if expected != result.research_record:
        raise ValueError("research history differs from its original accepted compilation")


def project_accepted_report(compiled: FindingReport, *, question: str, run_id: str, session_id: str,
                            project: str, synthesis_id: str) -> ResearchRecord:
    source_ids = {(row["id"], row["revision"]): stable_ref("source", row["id"], row["revision"])
                  for row in compiled.source_versions}
    sources = tuple(ResearchSource(source_ids[(row["id"], row["revision"])],
        requested_url_ref=sanitize_research_url_ref(row["url"]), final_url_ref=sanitize_research_url_ref(row["url"]),
        title_digest=digest_text(row["title"]), content_hash=row["content_hash"],
        retrieved_at=row["retrieved_at"], quality=row["quality"]) for row in compiled.source_versions)
    evidence = {}
    for row in compiled.evidence_bindings:
        identity = stable_ref("evidence", row["evidence_id"], row["evidence_revision"])
        sid = source_ids[(row["source_id"], row["source_revision"])]
        evidence[identity] = ResearchEvidence(identity, sid, row["excerpt_digest"], row["excerpt"],
            EvidenceLocator("knowledge_version", sid, page=row["page"], locator=row["locator"]),
            stance={"supported_by": "supports", "limited_by": "limits", "refuted_by": "contradicts"}[row["relation"]])
    claims = []
    assumptions = []
    for row in compiled.paragraph_bindings:
        if not row["refs"]:
            continue  # A declared gap is not a claim of contrary evidence.
        identity = stable_ref("claim", compiled.proposal_digest, row["path"])
        inference = "inference" in row["kinds"]
        selected = [ev for ev in compiled.evidence_bindings if ev["label"] in row["refs"]]
        # Premise evidence never becomes direct evidence for a candidate inference.
        refs = () if inference else tuple(dict.fromkeys(stable_ref("evidence", ev["evidence_id"], ev["evidence_revision"])
                                                       for ev in selected))
        assumption_refs: tuple[str, ...] = ()
        if inference:
            aid = stable_ref("assumption", identity)
            assumptions.append(ResearchAssumption(aid, row["text"], "candidate_inference_unreviewed", identity))
            assumption_refs = (aid,)
        claims.append(ResearchClaim(identity, row["text"], "conclusion" if row["path"] == "summary" else
            row["path"].split(".")[1].split("[")[0], evidence_refs=refs, assumption_refs=assumption_refs,
            status="assumption" if inference else "evidence_backed"))
    status = "answered" if all(row["state"] == "evidence_found" for row in compiled.coverage) else "partial"
    digest = digest_json(compiled.to_payload())
    return ResearchRecord(stable_ref("research_record", digest), digest,
        ResearchQuestion(stable_ref("question", question), digest_text(question), len(question)), status,
        sources=sources, evidence=tuple(evidence.values()), claims=tuple(claims), assumptions=tuple(assumptions),
        run_id=run_id, session_id=session_id, project_ref=project_ref(project), synthesis_id=synthesis_id, stop_reason="done")
