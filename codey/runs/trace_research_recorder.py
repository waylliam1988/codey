"""ResearchTraceRecorder: domain recording and deduplication with shared flush scheduling."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from codey.runs.trace_models import (
    RunTraceManifest,
)
from codey.runs.trace_research import (
    project_analysis_run,
    project_artifact_ref,
    project_evidence_ledger_write,
    project_planner_gap,
    project_reproducibility_capsule,
    project_research_brief_projection,
    project_research_connector_error,
    project_research_done_compilation,
    project_research_pipeline_result,
    project_research_plan,
    project_research_proof_review,
    project_research_record_summary,
    project_research_topic_continuity,
    project_review_finding,
    project_source_trust_row,
    source_ref_for_url,
)
from codey.runs.trace_schema import (
    MAX_ANALYSIS_RUNS,
    MAX_ARTIFACT_REFS,
    MAX_BRIEF_PROJECTIONS,
    MAX_EVIDENCE_LEDGER_WRITES,
    MAX_PLANNER_GAPS,
    MAX_REFS,
    MAX_REPRODUCIBILITY_CAPSULES,
    MAX_RESEARCH_CONNECTOR_ERRORS,
    MAX_RESEARCH_DONE_COMPILATIONS,
    MAX_RESEARCH_PIPELINE_RUNS,
    MAX_RESEARCH_PLANS,
    MAX_RESEARCH_PROOF_REVIEWS,
    MAX_RESEARCH_RECORDS,
    MAX_REVIEW_FINDINGS,
    MAX_SOURCE_TRUST_ROWS,
    MAX_TOPIC_CONTINUITY_ROWS,
)
from codey.runs.trace_values import _bounded_refs, _nonnegative_int, _trace_list_items
from codey.utils.refs import coerce_int


class ResearchTraceRecorder:
    def __init__(self, manifest: RunTraceManifest, *, checkpoint: Callable[[], None], flush: Callable[[], None]) -> None:
        self.manifest = manifest
        self.checkpoint = checkpoint
        self.flush = flush
        self._research_note_keys: set[str] = set()
        self._research_source_keys: set[str] = set()
        self._research_record_keys: set[str] = set()
        self._research_plan_keys: set[str] = set()
        self._analysis_run_keys: set[str] = set()
        self._artifact_version_keys: set[str] = set()
        self._capsule_keys: set[str] = set()
        self._review_finding_keys: set[str] = set()
        self._planner_gap_keys: set[str] = set()
        self._source_trust_keys: set[str] = set()
        self._brief_projection_keys: set[tuple[str, str]] = set()
        self._topic_continuity_keys: set[str] = set()

    def record_research_notes(self, ids: Iterable[object]) -> None:
        changed = False
        for note_id in _bounded_refs(ids):
            if note_id in self._research_note_keys:
                continue
            self._research_note_keys.add(note_id)
            self.manifest.research_note_ids.append(note_id)
            changed = True
        if changed:
            if len(self.manifest.research_note_ids) > MAX_REFS:
                del self.manifest.research_note_ids[:-MAX_REFS]
                self.manifest.warnings.append("research_note_ids_truncated")
            self.checkpoint()

    def record_research_sources(self, sources: Iterable[object]) -> None:
        changed = False
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            ref = source_ref_for_url(
                source.get("requested_url") or source.get("url"),
                final_url=source.get("final_url"),
                title=source.get("title"),
            )
            key = ref.get("url_digest", "")
            if not key or key in self._research_source_keys:
                continue
            self._research_source_keys.add(key)
            self.manifest.research_source_refs.append(ref)
            changed = True
        if changed:
            if len(self.manifest.research_source_refs) > MAX_REFS:
                del self.manifest.research_source_refs[:-MAX_REFS]
                self.manifest.warnings.append("research_source_refs_truncated")
            self.checkpoint()

    def record_research_record_summary(self, summary: Mapping[str, object]) -> None:
        payload = project_research_record_summary(summary)
        if payload is None:
            return
        key = str(payload["record_id"])
        if key in self._research_record_keys:
            return
        self._research_record_keys.add(key)
        self.manifest.research_records.append(payload)
        if len(self.manifest.research_records) > MAX_RESEARCH_RECORDS:
            del self.manifest.research_records[:-MAX_RESEARCH_RECORDS]
            self.manifest.warnings.append("research_records_truncated")
        self.checkpoint()

    def record_evidence_ledger_write(self, result: Mapping[str, object]) -> None:
        payload = project_evidence_ledger_write(result)
        if payload is None:
            return
        self.manifest.research_evidence_ledgers.append(payload)
        if len(self.manifest.research_evidence_ledgers) > MAX_EVIDENCE_LEDGER_WRITES:
            del self.manifest.research_evidence_ledgers[:-MAX_EVIDENCE_LEDGER_WRITES]
            self.manifest.warnings.append("research_evidence_ledgers_truncated")
        self.checkpoint()

    def record_research_proof_review(self, review: Mapping[str, object]) -> None:
        payload = project_research_proof_review(review)
        if payload is None:
            return
        review_key = (
            str(payload["proof_ref"]),
            str(payload.get("question_digest") or ""),
            tuple(_trace_list_items(payload.get("reason_codes"))),
        )
        for existing in self.manifest.research_proof_reviews:
            existing_key = (
                str(existing.get("proof_ref") or ""),
                str(existing.get("question_digest") or ""),
                tuple(_trace_list_items(existing.get("reason_codes"))),
            )
            if existing_key == review_key:
                return
        self.manifest.research_proof_reviews.append(payload)
        if len(self.manifest.research_proof_reviews) > MAX_RESEARCH_PROOF_REVIEWS:
            del self.manifest.research_proof_reviews[:-MAX_RESEARCH_PROOF_REVIEWS]
            self.manifest.warnings.append("research_proof_reviews_truncated")
        self.checkpoint()

    def record_research_plan(self, plan: Mapping[str, object]) -> None:
        payload = project_research_plan(plan)
        if payload is None:
            return
        plan_ref = str(payload["plan_ref"])
        if plan_ref in self._research_plan_keys:
            return
        self._research_plan_keys.add(plan_ref)
        self.manifest.research_plans.append(payload)
        if len(self.manifest.research_plans) > MAX_RESEARCH_PLANS:
            del self.manifest.research_plans[:-MAX_RESEARCH_PLANS]
            self.manifest.warnings.append("research_plans_truncated")
        self.checkpoint()

    def record_research_pipeline_result(self, result: object) -> None:
        payload = project_research_pipeline_result(result)
        if payload is None:
            return
        self.manifest.research_pipeline_runs.append(payload)

        if len(self.manifest.research_pipeline_runs) > MAX_RESEARCH_PIPELINE_RUNS:
            del self.manifest.research_pipeline_runs[:-MAX_RESEARCH_PIPELINE_RUNS]
            self.manifest.warnings.append("research_pipeline_runs_truncated")
        self.checkpoint()

    def record_research_connector_errors(self, errors: object) -> None:
        if not isinstance(errors, Iterable):
            return
        counts: dict[tuple[str, str, str], int] = {}
        for item in errors:
            row = project_research_connector_error(item)
            if row is None:
                continue
            key = (
                str(row["connector_id"]),
                str(row["action"]),
                str(row["error"]),
            )
            counts[key] = min(999, counts.get(key, 0) + coerce_int(row["count"]))
        if not counts:
            return
        existing = {
            (
                str(item.get("connector_id") or ""),
                str(item.get("action") or ""),
                str(item.get("error") or ""),
            ): item
            for item in self.manifest.research_connector_errors
            if isinstance(item, Mapping)
        }
        for key, count in counts.items():
            payload: dict[str, object] = {
                "connector_id": key[0],
                "action": key[1],
                "error": key[2],
                "count": count,
            }
            if key in existing:
                existing_item = existing[key]
                existing_item["count"] = min(999, _nonnegative_int(existing_item.get("count")) + count)
                continue
            self.manifest.research_connector_errors.append(payload)
        if len(self.manifest.research_connector_errors) > MAX_RESEARCH_CONNECTOR_ERRORS:
            del self.manifest.research_connector_errors[:-MAX_RESEARCH_CONNECTOR_ERRORS]
            self.manifest.warnings.append("research_connector_errors_truncated")
        self.flush()

    def record_research_done_compilation(self, result: Mapping[str, object]) -> None:
        payload = project_research_done_compilation(result)
        if payload is None:
            return
        self.manifest.research_done_compilations.append(payload)
        if len(self.manifest.research_done_compilations) > MAX_RESEARCH_DONE_COMPILATIONS:
            del self.manifest.research_done_compilations[:-MAX_RESEARCH_DONE_COMPILATIONS]
            self.manifest.warnings.append("research_done_compilations_truncated")
        self.checkpoint()

    def record_analysis_run(self, record: Mapping[str, object]) -> None:
        payload = project_analysis_run(record)
        if payload is None:
            return
        ref = str(payload["analysis_run_id"])
        if ref in self._analysis_run_keys:
            return
        self._analysis_run_keys.add(ref)
        self.manifest.analysis_runs.append(payload)
        if len(self.manifest.analysis_runs) > MAX_ANALYSIS_RUNS:
            del self.manifest.analysis_runs[:-MAX_ANALYSIS_RUNS]
            self.manifest.warnings.append("analysis_runs_truncated")
        self.checkpoint()

    def record_artifact_refs(self, refs: Iterable[object]) -> None:
        for item in _trace_list_items(refs):
            payload = project_artifact_ref(item)
            if payload is None:
                continue
            version_id = str(payload["version_id"])
            if version_id in self._artifact_version_keys:
                continue
            self._artifact_version_keys.add(version_id)
            self.manifest.artifact_refs.append(payload)
        if len(self.manifest.artifact_refs) > MAX_ARTIFACT_REFS:
            del self.manifest.artifact_refs[:-MAX_ARTIFACT_REFS]
            self.manifest.warnings.append("artifact_refs_truncated")
        self.checkpoint()

    def record_reproducibility_capsule(self, capsule: Mapping[str, object]) -> None:
        payload = project_reproducibility_capsule(capsule)
        if payload is None:
            return
        ref = str(payload["capsule_id"])
        # Capsules are aggregate snapshots of the same run: replace the stored
        # snapshot with the newest one instead of accumulating stale states.
        if ref in self._capsule_keys:
            self.manifest.reproducibility_capsules = [
                item
                for item in self.manifest.reproducibility_capsules
                if item.get("capsule_id") != ref
            ]
        else:
            self._capsule_keys.add(ref)
        self.manifest.reproducibility_capsules.append(payload)
        if len(self.manifest.reproducibility_capsules) > MAX_REPRODUCIBILITY_CAPSULES:
            del self.manifest.reproducibility_capsules[:-MAX_REPRODUCIBILITY_CAPSULES]
            self.manifest.warnings.append("reproducibility_capsules_truncated")
        self.checkpoint()

    def record_review_findings(self, findings: Iterable[object]) -> None:
        changed = False
        for item in _trace_list_items(findings):
            payload = project_review_finding(item)
            if payload is None:
                continue
            finding_id = str(payload["finding_id"])
            if finding_id in self._review_finding_keys:
                continue
            self._review_finding_keys.add(finding_id)
            self.manifest.research_review_findings.append(payload)
            changed = True
        if changed:
            if len(self.manifest.research_review_findings) > MAX_REVIEW_FINDINGS:
                del self.manifest.research_review_findings[:-MAX_REVIEW_FINDINGS]
                self.manifest.warnings.append("research_review_findings_truncated")
            self.checkpoint()

    def record_planner_gaps(self, gaps: Iterable[object]) -> None:
        changed = False
        for item in _trace_list_items(gaps):
            payload = project_planner_gap(item)
            if payload is None:
                continue
            gap_id = str(payload["gap_id"])
            if gap_id in self._planner_gap_keys:
                continue
            self._planner_gap_keys.add(gap_id)
            self.manifest.research_planner_gaps.append(payload)
            changed = True
        if changed:
            if len(self.manifest.research_planner_gaps) > MAX_PLANNER_GAPS:
                del self.manifest.research_planner_gaps[:-MAX_PLANNER_GAPS]
                self.manifest.warnings.append("research_planner_gaps_truncated")
            self.checkpoint()

    def record_research_source_trust(self, projections: Iterable[object]) -> None:
        """Record bounded source-trust projections (classes and refs only)."""

        changed = False
        for item in _trace_list_items(projections):
            payload = project_source_trust_row(item)
            if payload is None:
                continue
            source_ref = str(payload["source_ref"])
            if source_ref in self._source_trust_keys:
                continue
            self._source_trust_keys.add(source_ref)
            self.manifest.research_source_trust.append(payload)
            changed = True
        if changed:
            if len(self.manifest.research_source_trust) > MAX_SOURCE_TRUST_ROWS:
                del self.manifest.research_source_trust[:-MAX_SOURCE_TRUST_ROWS]
                self.manifest.warnings.append("research_source_trust_truncated")
            self.checkpoint()

    def record_research_brief_projection(self, projection: Mapping[str, object]) -> None:
        """Record one bounded research brief projection (refs + summaries)."""

        payload = project_research_brief_projection(projection)
        if payload is None:
            return
        key = (str(payload["record_ref"]), str(payload["record_digest"]))
        if key in self._brief_projection_keys:
            return
        self._brief_projection_keys.add(key)
        self.manifest.research_brief_projections.append(payload)
        if len(self.manifest.research_brief_projections) > MAX_BRIEF_PROJECTIONS:
            del self.manifest.research_brief_projections[:-MAX_BRIEF_PROJECTIONS]
            self.manifest.warnings.append("research_brief_projections_truncated")
        self.checkpoint()

    def record_research_topic_continuity(
        self,
        projection: Mapping[str, object],
        *,
        epoch_id: str,
    ) -> None:
        """Record one bounded topic-continuity admission (refs + counts only).

        The digest is the dedup and integrity anchor: rows without a valid
        content digest fail closed, and raw hint text has no field to live
        in — the trace stays refs-only by construction. The sent-bytes
        ``epoch_id`` binds this row to the exact outbound provider-send
        attempt whose intro carried the continuity section. It has no
        default and must be a well-formed ``ctx_epoch:<16 hex>`` ref;
        anything else fails closed without writing a row or touching the
        dedupe key, so an admitted row cannot exist outside a send-boundary
        binding — the trace never claims more than "these bytes left for
        the provider".
        """
        payload = project_research_topic_continuity(projection, epoch_id)
        if payload is None:
            return
        digest = str(payload["digest"])
        if digest in self._topic_continuity_keys:
            return
        self._topic_continuity_keys.add(digest)
        self.manifest.research_topic_continuity.append(payload)
        if len(self.manifest.research_topic_continuity) > MAX_TOPIC_CONTINUITY_ROWS:
            del self.manifest.research_topic_continuity[:-MAX_TOPIC_CONTINUITY_ROWS]
            self.manifest.warnings.append("research_topic_continuity_truncated")
        self.checkpoint()
