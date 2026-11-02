"""Immutable whole-answer compilation, with citation positions and no claim extraction."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference, canonical_json
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
from codey.research.answer_contract import ANSWER_STATES, citation_occurrences, digest, validate_submission
from codey.research.knowledge_updates import compile_updates, evidence_labels, normalize_updates

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
    citation_bindings: tuple[dict[str, Any], ...]
    source_versions: tuple[dict[str, Any], ...]
    evidence_bindings: tuple[dict[str, Any], ...]
    knowledge_refs: tuple[dict[str, Any], ...]
    knowledge_mutations: tuple[dict[str, Any], ...]
    attempt_refs: tuple[str, ...]
    selection_receipts: tuple[str, ...]
    questions: tuple[str, ...]
    knowledge_batch: dict[str, Any] | None = None

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return ()

    @property
    def coverage(self) -> tuple[dict[str, Any], ...]:
        return ()

    @property
    def open_questions(self) -> tuple[str, ...]:
        return self.questions if self.state != "answered" else ()

    def to_payload(self) -> dict[str, Any]:
        fields = asdict(self)
        if self.knowledge_batch is None:
            fields.pop("knowledge_batch")
        payload = json.loads(canonical_json({"schema": KNOWLEDGE_REPORT_SCHEMA if self.knowledge_batch is not None
                                             else REPORT_SCHEMA, **fields}))
        return {**payload, "compilation_digest": digest(payload)}

    @classmethod
    def from_payload(cls, raw: Any) -> AnswerReport:
        fields = set(cls.__dataclass_fields__) - {"knowledge_batch"}
        if isinstance(raw, dict) and raw.get("schema") == KNOWLEDGE_REPORT_SCHEMA:
            fields.add("knowledge_batch")
        if (not isinstance(raw, dict) or set(raw) != fields | {"schema", "compilation_digest"}
                or raw["schema"] not in {REPORT_SCHEMA, KNOWLEDGE_REPORT_SCHEMA}
                or raw["compilation_digest"] != digest({k: v for k, v in raw.items() if k != "compilation_digest"})):
            raise ValueError("invalid frozen whole-answer report")
        if (raw["state"] not in ANSWER_STATES or not isinstance(raw["markdown"], str)
                or not raw["markdown"].strip() or not raw["selection_receipts"] or not raw["attempt_refs"]
                or len(raw["questions"]) != 1 or digest(raw["questions"][0]) != raw["task_digest"]):
            raise ValueError("whole-answer report has no original task, submission or investigation")
        for row in raw["knowledge_refs"]:
            KnowledgeReference(row["id"], row["revision"], "uses")
        explicit: list[dict[str, Any]] = _batch_mutations(raw) if "knowledge_batch" in fields else []
        material = raw["knowledge_mutations"]
        if explicit:
            if material[-len(explicit):] != explicit:
                raise ValueError("knowledge mutations differ from the explicitly authored batch")
            material = material[:-len(explicit)]
        for row in material:
            mutation = KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
            if mutation.value.kind not in {"source", "evidence"}:
                raise ValueError("whole-answer preparation cannot manufacture claims or write a second answer")
        citations = citation_occurrences(raw["markdown"])
        if citations != [{key: row[key] for key in ("label", "start", "end")} for row in raw["citation_bindings"]]:
            raise ValueError("report citation locations differ from its original Markdown")
        bindings = {row["label"]: row for row in raw["evidence_bindings"]}
        labels = {row["label"] for row in citations}
        updates = raw.get("knowledge_batch", {}).get("updates", [])
        if (len(bindings) != len(raw["evidence_bindings"])
                or set(bindings) != labels | set(evidence_labels(updates))
                or not citations and raw["state"] != "unresolved"):
            raise ValueError("report citations differ from selected evidence")
        for row in raw["citation_bindings"]:
            if row["passage_ref"] != bindings[row["label"]]["passage_ref"]:
                raise ValueError("report citation points to another passage")
        if raw["knowledge_refs"] != [{"id": row["evidence_id"], "revision": row["evidence_revision"]}
                                     for row in raw["evidence_bindings"] if row["label"] in labels]:
            raise ValueError("answer dependencies differ from its body citations")
        if raw["text"] != render_answer(raw["markdown"], raw["source_versions"], raw["evidence_bindings"]):
            raise ValueError("report rendering differs from the frozen answer")
        return cls(raw["text"], raw["markdown"], raw["state"], raw["task_digest"], raw["proposal_digest"],
                   tuple(raw["citation_bindings"]), tuple(raw["source_versions"]), tuple(raw["evidence_bindings"]),
                   tuple(raw["knowledge_refs"]), tuple(raw["knowledge_mutations"]), tuple(raw["attempt_refs"]),
                   tuple(raw["selection_receipts"]), tuple(raw["questions"]), raw.get("knowledge_batch"))


def _batch_mutations(raw: dict[str, Any]) -> list[dict[str, Any]]:
    batch = raw["knowledge_batch"]
    if (not isinstance(batch, dict) or set(batch) != {"updates", "scope", "scope_id", "session_id"}
            or not isinstance(batch["session_id"], str)):
        raise ValueError("invalid frozen knowledge batch")
    updates = normalize_updates(batch["updates"], {row["label"] for row in raw["evidence_bindings"]})
    if not updates or batch["updates"] != updates:
        raise ValueError("knowledge batch must contain normalized authored entries")
    namespace = digest([raw["proposal_digest"], raw["selection_receipts"], batch["session_id"]])
    return compile_updates(updates, raw["evidence_bindings"], scope=batch["scope"], owner=batch["scope_id"],
                           namespace=namespace, submission_digest=raw["proposal_digest"])


def render_answer(markdown: str, sources: Any, evidence: Any) -> str:
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


def compile_answer_report(session: Any, ledger: Any, store: Any, *, project: str, session_id: str) -> AnswerReport:
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
    from codey.research.questions import INVESTIGATION_TOOLS

    for ref in scope["attempt_refs"]:
        attempt = session._memory_results.get(ref)
        if attempt is None or attempt.call.name not in INVESTIGATION_TOOLS:
            raise ValueError("submission has no original investigation receipt")
    mutations, sources, evidence = prepare_evidence(submission, ledger, store, project=project, session_id=session_id)
    batch = None
    if updates := submission.get("knowledge_updates"):
        scope_name, owner = prepared_scope(mutations, evidence, store, project=project, session_id=session_id)
        batch = {"updates": updates, "scope": scope_name, "scope_id": owner, "session_id": session_id}
        mutations.extend(compile_updates(updates, evidence, scope=scope_name, owner=owner,
            namespace=digest([submission["submission_digest"], [receipt], session_id]),
            submission_digest=submission["submission_digest"]))
    if len(mutations) >= 128:
        raise ValueError("knowledge updates exceed the original publication transaction budget")
    body_labels = {row["label"] for row in submission["citations"]}
    report = AnswerReport(render_answer(submission["markdown"], sources, evidence), submission["markdown"],
        submission["state"], submission["task_digest"], submission["submission_digest"], tuple(submission["citations"]),
        tuple(sources), tuple(evidence), tuple({"id": row["evidence_id"], "revision": row["evidence_revision"]}
                                             for row in evidence if row["label"] in body_labels),
        tuple(mutations), tuple(scope["attempt_refs"]), (receipt,), (session.task_text,), batch)
    return AnswerReport.from_payload(report.to_payload())
