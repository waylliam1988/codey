"""Immutable whole-answer compilation, with citation positions and no claim extraction."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
from codey.research.answer_contract import ANSWER_STATES, citation_occurrences, digest, validate_submission
from codey.research.knowledge_update_types import KnowledgeBatch, KnowledgeUpdate, MutationPayload
from codey.research.knowledge_updates import compile_updates, evidence_labels, normalize_updates
from codey.research.ledger import ResearchLedger
from codey.research.report_values import (
    CitationBinding,
    EvidenceBinding,
    SourceVersion,
    VersionReference,
    citation_binding,
    evidence_binding,
    mutation_payload,
    object_rows,
    source_version,
    strings,
    version_reference,
)
from codey.research.staging import StagedKnowledgeStore
from codey.reviews.research_coverage import CoverageRow
from codey.types.json_value import JsonObject, canonical_json_object, require_json_string

REPORT_SCHEMA = "research_answer_v1"
KNOWLEDGE_REPORT_SCHEMA = "research_answer_v2"
DELIVERY_CHECKS = RESEARCH_ANSWER_REQUIRED_CHECKS


@dataclass(frozen=True)
class AnswerReport:
    text: str
    markdown: str
    state: str
    task_digest: str
    proposal_digest: str
    citation_bindings: tuple[CitationBinding, ...]
    source_versions: tuple[SourceVersion, ...]
    evidence_bindings: tuple[EvidenceBinding, ...]
    knowledge_refs: tuple[VersionReference, ...]
    knowledge_mutations: tuple[MutationPayload, ...]
    attempt_refs: tuple[str, ...]
    selection_receipts: tuple[str, ...]
    questions: tuple[str, ...]
    knowledge_batch: KnowledgeBatch | None = None

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return ()

    @property
    def coverage(self) -> tuple[CoverageRow, ...]:
        return ()

    @property
    def open_questions(self) -> tuple[str, ...]:
        return self.questions if self.state != "answered" else ()

    def __post_init__(self) -> None:
        """Own detached wire containers; frozen prevents rebinding, not nested mutation."""
        object.__setattr__(self, "citation_bindings", deepcopy(self.citation_bindings))
        object.__setattr__(self, "source_versions", deepcopy(self.source_versions))
        object.__setattr__(self, "evidence_bindings", deepcopy(self.evidence_bindings))
        object.__setattr__(self, "knowledge_refs", deepcopy(self.knowledge_refs))
        object.__setattr__(self, "knowledge_mutations", deepcopy(self.knowledge_mutations))
        object.__setattr__(self, "knowledge_batch", deepcopy(self.knowledge_batch))

    def to_payload(self) -> JsonObject:
        fields = asdict(self)
        if self.knowledge_batch is None:
            fields.pop("knowledge_batch")
        payload = canonical_json_object({"schema": KNOWLEDGE_REPORT_SCHEMA if self.knowledge_batch is not None
                                             else REPORT_SCHEMA, **fields})
        return {**payload, "compilation_digest": digest(payload)}

    @classmethod
    def from_payload(cls, raw: object) -> AnswerReport:
        raw = canonical_json_object(raw)
        fields = set(cls.__dataclass_fields__) - {"knowledge_batch"}
        if raw.get("schema") == KNOWLEDGE_REPORT_SCHEMA:
            fields.add("knowledge_batch")
        schema = raw.get("schema")
        if (set(raw) != fields | {"schema", "compilation_digest"}
                or not isinstance(schema, str) or schema not in {REPORT_SCHEMA, KNOWLEDGE_REPORT_SCHEMA}
                or raw["compilation_digest"] != digest({k: v for k, v in raw.items() if k != "compilation_digest"})):
            raise ValueError("invalid frozen whole-answer report")
        evidence = tuple(evidence_binding(row) for row in object_rows(raw["evidence_bindings"], "evidence_bindings"))
        batch = _decode_batch(raw["knowledge_batch"], evidence) if "knowledge_batch" in fields else None
        report = cls(require_json_string(raw["text"], "text"), require_json_string(raw["markdown"], "markdown"),
            require_json_string(raw["state"], "state"), require_json_string(raw["task_digest"], "task_digest"),
            require_json_string(raw["proposal_digest"], "proposal_digest"),
            tuple(citation_binding(row) for row in object_rows(raw["citation_bindings"], "citation_bindings")),
            tuple(source_version(row) for row in object_rows(raw["source_versions"], "source_versions")), evidence,
            tuple(version_reference(row) for row in object_rows(raw["knowledge_refs"], "knowledge_refs")),
            tuple(mutation_payload(row) for row in object_rows(raw["knowledge_mutations"], "knowledge_mutations")),
            strings(raw["attempt_refs"], "attempt_refs"), strings(raw["selection_receipts"], "selection_receipts"),
            strings(raw["questions"], "questions"), batch)
        _validate_report(report)
        # A decoder must never silently strip fields from a digest-bound snapshot.
        if report.to_payload() != raw:
            raise ValueError("report contains noncanonical or unknown fields")
        return report


def _decode_batch(value: object, evidence: Sequence[EvidenceBinding]) -> KnowledgeBatch:
    batch = canonical_json_object(value)
    if set(batch) != {"updates", "scope", "scope_id", "session_id"}:
        raise ValueError("invalid frozen knowledge batch")
    updates = normalize_updates(batch["updates"], {row["label"] for row in evidence})
    if not updates or batch["updates"] != updates:
        raise ValueError("knowledge batch must contain normalized authored entries")
    return KnowledgeBatch(updates=updates, scope=require_json_string(batch["scope"], "scope"),
        scope_id=require_json_string(batch["scope_id"], "scope_id"),
        session_id=require_json_string(batch["session_id"], "session_id"))


def _validate_report(report: AnswerReport) -> None:
    if (report.state not in ANSWER_STATES or not report.markdown.strip()
            or not report.selection_receipts or not report.attempt_refs
            or len(report.questions) != 1 or digest(report.questions[0]) != report.task_digest):
        raise ValueError("whole-answer report has no original task, submission or investigation")
    material = report.knowledge_mutations
    if report.knowledge_batch is not None:
        batch = report.knowledge_batch
        explicit = compile_updates(batch["updates"], report.evidence_bindings, scope=batch["scope"], owner=batch["scope_id"],
            namespace=digest([report.proposal_digest, report.selection_receipts, batch["session_id"]]),
            submission_digest=report.proposal_digest)
        if material[-len(explicit):] != tuple(explicit):
            raise ValueError("knowledge mutations differ from the explicitly authored batch")
        material = material[:-len(explicit)]
    for row in material:
        mutation = KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
        if mutation.value.kind not in {"source", "evidence"}:
            raise ValueError("whole-answer preparation cannot manufacture claims or write a second answer")
    citations = citation_occurrences(report.markdown)
    if citations != [{"label": row["label"], "start": row["start"], "end": row["end"]} for row in report.citation_bindings]:
        raise ValueError("report citation locations differ from its original Markdown")
    bindings = {row["label"]: row for row in report.evidence_bindings}
    labels = {row["label"] for row in report.citation_bindings}
    updates: list[KnowledgeUpdate] = report.knowledge_batch["updates"] if report.knowledge_batch is not None else []
    if (len(bindings) != len(report.evidence_bindings) or set(bindings) != labels | set(evidence_labels(updates))
            or not citations and report.state != "unresolved"):
        raise ValueError("report citations differ from selected evidence")
    for citation in report.citation_bindings:
        if citation["passage_ref"] != bindings[citation["label"]].get("passage_ref"):
            raise ValueError("report citation points to another passage")
    if list(report.knowledge_refs) != [{"id": row["evidence_id"], "revision": row["evidence_revision"]}
                                     for row in report.evidence_bindings if row["label"] in labels]:
        raise ValueError("answer dependencies differ from its body citations")
    if report.text != render_answer(report.markdown, report.source_versions, report.evidence_bindings):
        raise ValueError("report rendering differs from the frozen answer")


def render_answer(markdown: str, sources: Sequence[SourceVersion], evidence: Sequence[EvidenceBinding]) -> str:
    labels = {row["label"] for row in citation_occurrences(markdown)}
    evidence = [row for row in evidence if row["label"] in labels]
    if not evidence:
        return markdown
    by_id = {(row["id"], row["revision"]): row for row in sources}
    rows = []
    for item in evidence:
        source = by_id[item["source_id"], item["source_revision"]]
        title = source["title"].replace("\n", " ").replace("[", "").replace("]", "")
        rows.append(f"[{item['label']}] {title} — {source['url']} "
                    f"(source {source['content_hash']}, {item['start']}:{item['end']})")
    return markdown + "\n\n## 引用来源\n" + "\n".join(rows)


def compile_answer_report(session: TaskSession, ledger: ResearchLedger, store: KnowledgeStore | StagedKnowledgeStore, *, project: str, session_id: str) -> AnswerReport:
    from codey.research.answer_archive import prepare_evidence, prepared_scope

    found = [(ref, result) for ref, result in session._memory_results.items()
             if result.ok and result.call.name == "submit_research_answer" and "research_submission" in result.canonical]
    if not found:
        raise ValueError("whole research answer has not been submitted")
    receipt, result = found[-1]
    submission = validate_submission(result.canonical["research_submission"], session.task_text)
    from codey.research.answer_contract import AnswerScope, submit_answer

    if submit_answer(AnswerScope.from_payload(submission["scope"]), result.call.args) != submission:
        raise ValueError("settled submission differs from the original tool arguments")
    scope = submission["scope"]
    # A receipt may never invent investigations just by carrying a matching digest.
    from codey.research.questions import is_investigation_tool

    for ref in scope["attempt_refs"]:
        attempt = session._memory_results.get(ref)
        if attempt is None or not is_investigation_tool(attempt.call.name):
            raise ValueError("submission has no original investigation receipt")
    mutations, sources, evidence = prepare_evidence(submission, ledger, store, project=project, session_id=session_id)
    batch: KnowledgeBatch | None = None
    if updates := submission.get("knowledge_updates"):
        scope_name, owner = prepared_scope(mutations, evidence, store, project=project, session_id=session_id)
        batch = {"updates": updates, "scope": scope_name, "scope_id": owner, "session_id": session_id}
        mutations.extend(compile_updates(updates, evidence, scope=scope_name, owner=owner,
            namespace=digest([submission["submission_digest"], [receipt], session_id]),
            submission_digest=submission["submission_digest"]))
    if len(mutations) >= 128:
        raise ValueError("knowledge updates exceed the original publication transaction budget")
    body_labels = {row["label"] for row in submission["citations"]}
    report = AnswerReport(render_answer(submission["markdown"], tuple(source_version(row) for row in sources), tuple(evidence_binding(row) for row in evidence)), submission["markdown"],
        submission["state"], submission["task_digest"], submission["submission_digest"], tuple(citation_binding(row) for row in submission["citations"]),
        tuple(source_version(row) for row in sources), tuple(evidence_binding(row) for row in evidence), tuple({"id": row["evidence_id"], "revision": row["evidence_revision"]}
                                             for row in evidence if row["label"] in body_labels),
        tuple(mutation_payload(row) for row in mutations), tuple(scope["attempt_refs"]), (receipt,), (session.task_text,), batch)
    return AnswerReport.from_payload(report.to_payload())
