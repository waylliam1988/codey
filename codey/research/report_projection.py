"""ResearchRecord delivery projection from the accepted compilation only."""

from codey.completion.contract import (
    CHECK_PASS,
    CompletionCheck,
    CompletionProof,
    build_completion_contract,
    project_completion_proof,
)
from codey.research.answer_report import DELIVERY_CHECKS, AnswerReport
from codey.research.identity import project_ref, sanitize_research_url_ref
from codey.research.object_model import (
    EvidenceLocator,
    ResearchEvidence,
    ResearchQuestion,
    ResearchRecord,
    ResearchSource,
)
from codey.research.report_values import has_project_delivery, report_delivery_matches
from codey.research.run_result import ResearchRunResult
from codey.types.json_value import require_json_string
from codey.utils.refs import digest_json, digest_text, stable_ref

PUBLICATION_CHECK = CompletionCheck("research_publication", CHECK_PASS, "settled_publication")


def published_completion(proof: CompletionProof, compiled: AnswerReport, *,
                         synthesis_id: str, result_ref: str, answer_version: tuple[str, int] | None = None) -> CompletionProof:
    """Bind deterministic writer checks to its settled publication, never a model verdict."""
    if not isinstance(proof, CompletionProof) or not proof.satisfied or not synthesis_id or not result_ref:
        raise ValueError("research publication requires its completed writer and original result")
    if answer_version is None:
        raise ValueError("whole-answer publication requires its settled knowledge version")
    answer_refs = (stable_ref("knowledge_answer", *answer_version),)
    contract = build_completion_contract(domain=proof.domain, subject_ref=proof.subject_ref,
        checks=(*proof.checks, PUBLICATION_CHECK),
        evidence_refs=(stable_ref("research_publication", synthesis_id), *answer_refs, *proof.evidence_refs),
        artifact_refs=("research-report:" + require_json_string(compiled.to_payload()["compilation_digest"], "compilation_digest"), result_ref, *proof.artifact_refs),
        limitation_refs=proof.limitation_refs, finding_refs=proof.finding_refs,
        analysis_run_refs=proof.analysis_run_refs, external_refs=proof.external_refs, diagnostic_refs=proof.diagnostic_refs)
    combined = project_completion_proof(contract) if contract is not None else None
    if combined is None or not combined.satisfied:
        raise ValueError("research publication proof is incomplete")
    return combined


def validate_accepted_projection(result: ResearchRunResult, *, run_id: str, session_id: str, project: str) -> None:
    from codey.knowledge.archive import stable_identity

    compiled = result.accepted_report
    if not isinstance(compiled, AnswerReport):
        raise ValueError("accepted projection requires a compiled report")
    AnswerReport.from_payload(compiled.to_payload())
    if compiled.questions != (result.question,):
        raise ValueError("whole answer belongs to another task")
    raw_proof = result.completion_proof
    proof = raw_proof if isinstance(raw_proof, CompletionProof) else None
    required = {*DELIVERY_CHECKS, "research_publication"}
    answer_ref = stable_ref("knowledge_answer", stable_identity("answer", run_id, "initial"), 1)
    if proof is None or answer_ref not in proof.evidence_refs:
        raise ValueError("whole-answer proof lost its exact published version")
    if (not report_delivery_matches(result.summary, compiled.text, combined=has_project_delivery(proof))
            or result.stop_reason != "done"
            or not isinstance(proof, CompletionProof) or not proof.satisfied
            or proof.domain != "research" or proof.subject_ref != f"run:{run_id[:80]}:task:research"
            or not required <= {row.check_id for row in proof.checks if row.status == "pass"}
            or any(row.reason_code == "same_provider_model_assessment" for row in proof.checks)
            or not result.synthesis_id or stable_ref("research_publication", result.synthesis_id) not in proof.evidence_refs
            or "research-report:" + require_json_string(compiled.to_payload()["compilation_digest"], "compilation_digest") not in proof.artifact_refs):
        raise ValueError("compiled report differs from its deterministic publication proof")
    contract = build_completion_contract(domain=proof.domain, subject_ref=proof.subject_ref, checks=proof.checks,
        evidence_refs=proof.evidence_refs, limitation_refs=proof.limitation_refs, finding_refs=proof.finding_refs,
        analysis_run_refs=proof.analysis_run_refs, artifact_refs=proof.artifact_refs,
        external_refs=proof.external_refs, diagnostic_refs=proof.diagnostic_refs)
    if project_completion_proof(contract) != proof:
        raise ValueError("completion proof identity differs from its original inputs")
    expected = project_accepted_report(compiled, question=result.question, run_id=run_id, session_id=session_id,
                                       project=project, synthesis_id=result.synthesis_id)
    if expected != result.research_record:
        raise ValueError("research history differs from its original compilation")


def project_accepted_report(compiled: AnswerReport, *, question: str, run_id: str, session_id: str,
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
