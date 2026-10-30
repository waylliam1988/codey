"""ResearchRecord delivery projection from the accepted compilation only."""

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
