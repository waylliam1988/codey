"""ResearchRecord delivery projection from the accepted compilation only."""

from typing import Any

from codey.completion.contract import CompletionProof, build_completion_contract, project_completion_proof
from codey.research.finding_report import FindingReport, ResearchReport
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


def published_completion(proof: CompletionProof, compiled: ResearchReport, *,
                         synthesis_id: str, result_ref: str, answer_version: tuple[str, int] | None = None) -> CompletionProof:
    """Bind deterministic writer checks to its settled publication, never a model verdict."""
    from codey.completion.contract import CHECK_PASS, CompletionCheck

    if not isinstance(proof, CompletionProof) or not proof.satisfied or not synthesis_id or not result_ref:
        raise ValueError("research publication requires its completed writer and original result")
    refs = {name: getattr(proof, name) for name in ("limitation_refs", "finding_refs", "analysis_run_refs",
                                                  "external_refs", "diagnostic_refs")}
    from codey.research.answer_report import AnswerReport

    if isinstance(compiled, AnswerReport) and answer_version is None:
        raise ValueError("whole-answer publication requires its settled knowledge version")
    answer_refs = (stable_ref("knowledge_answer", *answer_version),) if answer_version else ()
    contract = build_completion_contract(domain=proof.domain, subject_ref=proof.subject_ref,
        checks=(*proof.checks, CompletionCheck("research_publication", CHECK_PASS, "settled_publication")),
        evidence_refs=(stable_ref("research_publication", synthesis_id), *answer_refs, *proof.evidence_refs),
        artifact_refs=("research-report:" + compiled.to_payload()["compilation_digest"], result_ref, *proof.artifact_refs),
        **refs)
    combined = project_completion_proof(contract) if contract is not None else None
    if combined is None or not combined.satisfied:
        raise ValueError("research publication proof is incomplete")
    return combined


def validate_accepted_projection(result: Any, *, run_id: str, session_id: str, project: str) -> None:
    compiled = result.accepted_report
    FindingReport.from_payload(compiled.to_payload())
    from codey.research.answer_report import DELIVERY_CHECKS, AnswerReport
    from codey.research.task_requirements import validate_task_accounting

    if not isinstance(compiled, AnswerReport):
        validate_task_accounting(list(compiled.coverage), result.question)
    elif compiled.questions != (result.question,):
        raise ValueError("whole answer belongs to another task")
    proof = result.completion_proof
    required = {"research_citations", "research_sources_opened", "research_evidence_saved",
                "research_report_sections", "research_claim_bindings", "research_question_coverage",
                "research_task_accounting", "research_publication"}
    if isinstance(compiled, AnswerReport):
        from codey.knowledge.archive import stable_identity

        required = {*DELIVERY_CHECKS, "research_publication"}
        # New whole answers publish once per ordinary run; edits use explicit
        # knowledge revisions, never reinterpret this original frozen command.
        answer_ref = stable_ref("knowledge_answer", stable_identity("answer", run_id, "initial"), 1)
        if answer_ref not in getattr(proof, "evidence_refs", ()):
            raise ValueError("whole-answer proof lost its exact published version")
    if (result.summary != compiled.text or result.stop_reason != "done"
            or not isinstance(proof, CompletionProof) or not proof.satisfied
            or proof.domain != "research" or proof.subject_ref != f"run:{run_id[:80]}:task:research"
            or not required <= {row.check_id for row in proof.checks if row.status == "pass"}
            or any(row.reason_code == "same_provider_model_assessment" for row in proof.checks)
            or not result.synthesis_id or stable_ref("research_publication", result.synthesis_id) not in proof.evidence_refs
            or "research-report:" + compiled.to_payload()["compilation_digest"] not in proof.artifact_refs):
        raise ValueError("compiled report differs from its deterministic publication proof")
    contract = build_completion_contract(domain=proof.domain, subject_ref=proof.subject_ref, checks=proof.checks,
        **{name: getattr(proof, name) for name in ("evidence_refs", "limitation_refs", "finding_refs",
            "analysis_run_refs", "artifact_refs", "external_refs", "diagnostic_refs")})
    if project_completion_proof(contract) != proof:
        raise ValueError("completion proof identity differs from its original inputs")
    expected = project_accepted_report(compiled, question=result.question, run_id=run_id, session_id=session_id,
                                       project=project, synthesis_id=result.synthesis_id)
    if expected != result.research_record:
        raise ValueError("research history differs from its original compilation")


def project_accepted_report(compiled: ResearchReport, *, question: str, run_id: str, session_id: str,
                            project: str, synthesis_id: str) -> ResearchRecord:
    from codey.research.answer_report import AnswerReport

    if isinstance(compiled, AnswerReport):
        return project_whole_answer(compiled, question=question, run_id=run_id, session_id=session_id,
                                    project=project, synthesis_id=synthesis_id)
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


def project_whole_answer(compiled: Any, *, question: str, run_id: str, session_id: str,
                         project: str, synthesis_id: str) -> ResearchRecord:
    # Material saved only for an explicit knowledge update is not a body citation.
    labels = {row["label"] for row in compiled.citation_bindings}
    selected = [row for row in compiled.evidence_bindings if row["label"] in labels]
    cited_sources = {(row["source_id"], row["source_revision"]) for row in selected}
    versions = [row for row in compiled.source_versions if (row["id"], row["revision"]) in cited_sources]
    source_ids = {(row["id"], row["revision"]): stable_ref("source", row["id"], row["revision"])
                  for row in versions}
    sources = tuple(ResearchSource(source_ids[(row["id"], row["revision"])],
        requested_url_ref=sanitize_research_url_ref(row["url"]), final_url_ref=sanitize_research_url_ref(row["url"]),
        title_digest=digest_text(row["title"]), content_hash=row["content_hash"],
        retrieved_at=row["retrieved_at"], quality=row["quality"]) for row in versions)
    evidence = []
    for row in selected:
        sid = source_ids[(row["source_id"], row["source_revision"])]
        evidence.append(ResearchEvidence(stable_ref("evidence", row["evidence_id"], row["evidence_revision"]),
            sid, row["excerpt_digest"], row["excerpt"], EvidenceLocator("knowledge_version", sid, page=row["page"],
                char_start=row["start"], char_end=row["end"], locator=row["locator"]), stance="cited_unreviewed"))
    record_digest = digest_json(compiled.to_payload())
    return ResearchRecord(stable_ref("research_record", record_digest), record_digest,
        ResearchQuestion(stable_ref("question", question), digest_text(question), len(question)),
        "insufficient_evidence" if compiled.state == "unresolved" else compiled.state,
        sources=sources, evidence=tuple(evidence), claims=(), assumptions=(),
        run_id=run_id, session_id=session_id, project_ref=project_ref(project), synthesis_id=synthesis_id, stop_reason="done")
