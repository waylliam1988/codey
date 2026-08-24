"""Bounded audit sidecar for one Codey run.

Run Trace is an index over model-visible inputs and local runtime choices.  It
is deliberately not a transcript and not a second execution ledger: raw prompt
text, chat text, source bodies, webpage bodies, and provider raw errors do not
belong here.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from codey.local_store import DEFAULT_STATE_HOME, session_key, write_json_atomic
from codey.completion_contract import (
    CHECK_STATUSES as _COMPLETION_CHECK_STATUSES,
    COMPLETION_COMPLETE_WITH_LIMITATIONS as _COMPLETION_COMPLETE_WITH_LIMITATIONS,
    COMPLETION_SATISFIED_STATUSES as _COMPLETION_SATISFIED_STATUSES,
    COMPLETION_DOMAINS as _COMPLETION_TRACE_DOMAINS,
    COMPLETION_STATUSES as _COMPLETION_TRACE_STATUSES,
    MAX_COMPLETION_CHECKS as _MAX_COMPLETION_CHECKS,
)
from codey.context_epoch import admission_from_rendered_source
from codey.prompt_envelope import is_model_boundary_freshness
from codey.research.artifact_lineage import is_valid_derived_ref
from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref
from codey.redaction import looks_sensitive_code, looks_sensitive_signal
from codey.research.review_finding import (
    FINDING_KINDS,
    FINDING_SEVERITIES,
    FINDING_STATUSES,
    GAP_KINDS,
    SEVERITY_WARNING,
    STATUS_OPEN,
)
from codey.research.source_trust import SOURCE_CLASSES as _SOURCE_TRUST_CLASSES
from codey.research.shape import valid_digest_ref
from codey.research.shape import generated_ref as _generated_ref
from codey.research.shape import safe_connector_id as _safe_connector_id


SCHEMA_VERSION = 1
TRACE_KIND = "run_trace_manifest"
MAX_TRACE_BYTES = 256 * 1024
MAX_TEXT_CHARS = 240
MAX_PROMPT_SECTIONS = 80
MAX_REFS = 64
MAX_FALLBACKS = 16
MAX_FAILURES = 16
MAX_WARNINGS = 16
MAX_PERMISSION_PROFILES = 16
MAX_TOOL_CONTRACTS = 16
MAX_POLICY_DECISIONS = 80
MAX_RESEARCH_RECORDS = 8
MAX_EVIDENCE_LEDGER_WRITES = 8
MAX_RESEARCH_PROOF_REVIEWS = 8
MAX_RESEARCH_PLANS = 8
MAX_RESEARCH_PIPELINE_RUNS = 8
MAX_RESEARCH_CONNECTOR_ERRORS = 8
MAX_RESEARCH_DONE_COMPILATIONS = 8
MAX_ANALYSIS_RUNS = 8
MAX_ARTIFACT_REFS = 16
MAX_REPRODUCIBILITY_CAPSULES = 8
MAX_CAPSULE_ARTIFACT_REFS = 8
MAX_REVIEW_FINDINGS = 16
MAX_PLANNER_GAPS = 16
MAX_GAP_FINDING_REFS = 4
MAX_COMPLETION_PROOFS = 8
MAX_COMPLETION_CHECK_ROWS = _MAX_COMPLETION_CHECKS
MAX_SOURCE_TRUST_ROWS = 32
MAX_SOURCE_TRUST_CLASSES = 3
MAX_BRIEF_PROJECTIONS = 8
MAX_BRIEF_CLAIM_ROWS = 16
MAX_BRIEF_REFS = 24
CHECKPOINT_FLUSH_INTERVAL = 8
TRUNCATED_TEXT_SUFFIX = "..."
REVIEW_FINDING_REF_KINDS: dict[str, str] = {
    "claim_ref": "claim",
    "evidence_ref": "evidence",
    "source_ref": "source",
    "analysis_run_ref": "analysis_run",
    "artifact_ref": "artifact_version",
    "proof_ref": "research_proof",
}
RESEARCH_ANSWER_STATUSES = frozenset({
    "answered",
    "partial",
    "insufficient_evidence",
    "not_answered",
})


def digest_text(value: object) -> str:
    text = str(value or "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def project_ref(project: str | Path | None) -> dict[str, str]:
    text = str(project or "").strip()
    if not text:
        return {}
    try:
        resolved = Path(text).expanduser().resolve()
        basename = resolved.name
        digest_source = os.path.normcase(str(resolved))
    except (OSError, RuntimeError, ValueError):
        path = Path(text)
        basename = path.name or _clip(text, 80)
        digest_source = text
    return {
        "basename": _clip(basename, 80),
        "digest": digest_text(digest_source),
    }


def source_ref_for_url(url: object, *, final_url: object = "", title: object = "") -> dict[str, str]:
    requested = str(url or "").strip()
    final = str(final_url or "").strip()
    chosen = final or requested
    if not chosen:
        return {}
    host = _host(chosen or requested)
    payload = {
        "url_digest": digest_text(chosen),
    }
    if host:
        payload["host"] = _clip(host, 120)
    if requested and final and requested != final:
        payload["requested_digest"] = digest_text(requested)
        payload["final_digest"] = digest_text(final)
    text_title = _clip(title, 160)
    if text_title:
        payload["title_digest"] = digest_text(text_title)
    return payload


@dataclass(frozen=True)
class PromptSectionTrace:
    name: str
    digest: str
    chars: int
    purpose: str = ""
    model_visible: bool = True
    budget: int = 0
    truncated: bool = False
    freshness: str = ""
    source_refs: tuple[str, ...] = ()
    epoch_id: str = ""
    admission_reason: str = ""
    capability_id: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": _identifier(self.name, 80),
            "digest": self.digest,
            "chars": max(0, int(self.chars or 0)),
            "model_visible": bool(self.model_visible),
            "truncated": bool(self.truncated),
        }
        if self.purpose:
            payload["purpose"] = _clip(self.purpose, 160)
        if self.budget:
            payload["budget"] = max(0, int(self.budget or 0))
        if self.freshness:
            payload["freshness"] = _identifier(self.freshness, 80)
        refs = _bounded_refs(self.source_refs)
        if refs:
            payload["source_refs"] = list(refs)
        if self.epoch_id:
            payload["epoch_id"] = _identifier(self.epoch_id, 80)
        if self.admission_reason:
            payload["admission_reason"] = _identifier(self.admission_reason, 80)
        if self.capability_id:
            payload["capability_id"] = _identifier(self.capability_id, 80)
        return payload


@dataclass(frozen=True)
class RouterTrace:
    baseline_mode: str = ""
    selected_mode: str = ""
    final_mode: str = ""
    source: str = ""
    reason_code: str = ""
    overridden_by_user: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "baseline_mode": _identifier(self.baseline_mode, 40),
            "selected_mode": _identifier(self.selected_mode, 40),
            "final_mode": _identifier(self.final_mode, 40),
            "source": _identifier(self.source, 80),
            "reason_code": _identifier(self.reason_code, 120),
            "overridden_by_user": bool(self.overridden_by_user),
        }


@dataclass(frozen=True)
class FallbackTrace:
    from_provider: str
    to_provider: str
    phase: str
    reason_code: str

    def to_payload(self) -> dict[str, object]:
        return {
            "from_provider": _identifier(self.from_provider, 80),
            "to_provider": _identifier(self.to_provider, 80),
            "phase": _identifier(self.phase, 80),
            "reason_code": _identifier(self.reason_code, 120),
        }


@dataclass
class RunTraceManifest:
    run_id: str
    session_id: str
    project_ref: Mapping[str, str] = field(default_factory=dict)
    mode_initial: str = ""
    mode_final: str = ""
    provider_initial: str = ""
    provider_final: str = ""
    permission_profile: str = ""
    permission_profiles: list[dict[str, str]] = field(default_factory=list)
    router: RouterTrace | None = None
    prompt_sections: list[PromptSectionTrace] = field(default_factory=list)
    model_tool_contract_hash: str = ""
    runtime_tool_contract_hash: str = ""
    tool_contracts: list[dict[str, str]] = field(default_factory=list)
    local_context_refs: list[dict[str, object]] = field(default_factory=list)
    research_note_ids: list[str] = field(default_factory=list)
    research_source_refs: list[dict[str, str]] = field(default_factory=list)
    research_records: list[dict[str, object]] = field(default_factory=list)
    research_evidence_ledgers: list[dict[str, object]] = field(default_factory=list)
    research_proof_reviews: list[dict[str, object]] = field(default_factory=list)
    research_plans: list[dict[str, object]] = field(default_factory=list)
    research_pipeline_runs: list[dict[str, object]] = field(default_factory=list)
    research_connector_errors: list[dict[str, object]] = field(default_factory=list)
    research_done_compilations: list[dict[str, object]] = field(default_factory=list)
    analysis_runs: list[dict[str, object]] = field(default_factory=list)
    artifact_refs: list[dict[str, object]] = field(default_factory=list)
    reproducibility_capsules: list[dict[str, object]] = field(default_factory=list)
    research_review_findings: list[dict[str, object]] = field(default_factory=list)
    research_planner_gaps: list[dict[str, object]] = field(default_factory=list)
    completion_proofs: list[dict[str, object]] = field(default_factory=list)
    research_source_trust: list[dict[str, object]] = field(default_factory=list)
    research_brief_projections: list[dict[str, object]] = field(default_factory=list)
    fallbacks: list[FallbackTrace] = field(default_factory=list)
    provider_failures: list[dict[str, str]] = field(default_factory=list)
    policy_decisions: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "running"

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "kind": TRACE_KIND,
            "run_id": _clip(self.run_id, 120),
            "session_id": _clip(self.session_id, 120),
            "project_ref": dict(self.project_ref),
            "mode_initial": _identifier(self.mode_initial, 40),
            "mode_final": _identifier(self.mode_final, 40),
            "provider_initial": _identifier(self.provider_initial, 80),
            "provider_final": _identifier(self.provider_final, 80),
            "permission_profile": _identifier(self.permission_profile, 80),
            "permission_profiles": self.permission_profiles[:MAX_PERMISSION_PROFILES],
            "router": self.router.to_payload() if self.router else {},
            "prompt_sections": [
                item.to_payload() for item in self.prompt_sections[:MAX_PROMPT_SECTIONS]
            ],
            "model_tool_contract_hash": _clip(self.model_tool_contract_hash, 80),
            "runtime_tool_contract_hash": _clip(self.runtime_tool_contract_hash, 80),
            "tool_contracts": self.tool_contracts[:MAX_TOOL_CONTRACTS],
            "local_context_refs": self.local_context_refs[:MAX_REFS],
            "research_note_ids": list(_bounded_refs(self.research_note_ids)),
            "research_source_refs": self.research_source_refs[:MAX_REFS],
            "research_records": self.research_records[:MAX_RESEARCH_RECORDS],
            "research_evidence_ledgers": (
                self.research_evidence_ledgers[:MAX_EVIDENCE_LEDGER_WRITES]
            ),
            "research_proof_reviews": (
                self.research_proof_reviews[:MAX_RESEARCH_PROOF_REVIEWS]
            ),
            "research_plans": self.research_plans[:MAX_RESEARCH_PLANS],
            "research_pipeline_runs": (
                self.research_pipeline_runs[:MAX_RESEARCH_PIPELINE_RUNS]
            ),
            "research_connector_errors": [
                _research_connector_error_payload(item)
                for item in self.research_connector_errors[:MAX_RESEARCH_CONNECTOR_ERRORS]
            ],
            "research_done_compilations": (
                self.research_done_compilations[:MAX_RESEARCH_DONE_COMPILATIONS]
            ),
            "analysis_runs": self.analysis_runs[:MAX_ANALYSIS_RUNS],
            "artifact_refs": self.artifact_refs[:MAX_ARTIFACT_REFS],
            "reproducibility_capsules": (
                self.reproducibility_capsules[:MAX_REPRODUCIBILITY_CAPSULES]
            ),
            "research_review_findings": self.research_review_findings[:MAX_REVIEW_FINDINGS],
            "research_planner_gaps": self.research_planner_gaps[:MAX_PLANNER_GAPS],
            "completion_proofs": self.completion_proofs[:MAX_COMPLETION_PROOFS],
            "research_source_trust": self.research_source_trust[:MAX_SOURCE_TRUST_ROWS],
            "research_brief_projections": (
                self.research_brief_projections[:MAX_BRIEF_PROJECTIONS]
            ),
            "fallbacks": [item.to_payload() for item in self.fallbacks[:MAX_FALLBACKS]],
            "provider_failures": self.provider_failures[:MAX_FAILURES],
            "policy_decisions": self.policy_decisions[:MAX_POLICY_DECISIONS],
            "warnings": list(_bounded_refs(self.warnings, limit=MAX_WARNINGS)),
            "status": _identifier(self.status, 40),
        }
        return payload


class RunTraceStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, session_id: str, run_id: str) -> Path:
        return self.session_dir(session_id) / f"{_safe_file_stem(run_id)}.json"

    def session_dir(self, session_id: str) -> Path:
        return self.state_home / "run_traces" / session_key(session_id)

    def open(
        self,
        *,
        run_id: str,
        session_id: str,
        project: str | Path | None,
        mode_initial: str,
        provider_initial: str,
    ) -> "RunTraceRecorder":
        manifest = RunTraceManifest(
            run_id=_clip(run_id, 120),
            session_id=_clip(session_id, 120),
            project_ref=project_ref(project),
            mode_initial=_identifier(mode_initial, 40),
            mode_final=_identifier(mode_initial, 40),
            provider_initial=_identifier(provider_initial, 80),
            provider_final=_identifier(provider_initial, 80),
        )
        recorder = RunTraceRecorder(self.path_for(session_id, run_id), manifest)
        recorder.flush()
        return recorder

    def delete_session(self, session_id: str) -> None:
        root = (self.state_home / "run_traces").resolve()
        directory = self.session_dir(session_id)
        try:
            if directory.parent.resolve() != root:
                return
            if directory.is_symlink():
                return
        except (OSError, RuntimeError, ValueError):
            return
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return
        except OSError:
            return


class RunTraceRecorder:
    def __init__(self, path: Path, manifest: RunTraceManifest) -> None:
        self.path = path
        self.manifest = manifest
        self.disabled = False
        self._dirty_updates = 0
        self._prompt_keys: set[
            tuple[str, str, str, str, tuple[str, ...], bool, str]
        ] = set()
        self._local_context_keys: set[tuple[str, str, str]] = set()
        self._research_note_keys: set[str] = set()
        self._research_source_keys: set[str] = set()
        self._research_record_keys: set[str] = set()
        self._research_plan_keys: set[str] = set()
        self._analysis_run_keys: set[str] = set()
        self._artifact_version_keys: set[str] = set()
        self._capsule_keys: set[str] = set()
        self._review_finding_keys: set[str] = set()
        self._planner_gap_keys: set[str] = set()
        self._completion_proof_keys: set[str] = set()
        self._source_trust_keys: set[str] = set()
        self._brief_projection_keys: set[str] = set()
        self._policy_keys: set[tuple[str, str, str, str, str]] = set()

    def record_router(
        self,
        *,
        baseline_mode: str,
        selected_mode: str,
        final_mode: str,
        source: str,
        reason_code: str,
        overridden_by_user: bool = False,
    ) -> None:
        self.manifest.router = RouterTrace(
            baseline_mode=baseline_mode,
            selected_mode=selected_mode,
            final_mode=final_mode,
            source=source,
            reason_code=reason_code,
            overridden_by_user=overridden_by_user,
        )
        self.manifest.mode_final = _identifier(final_mode, 40)
        self.flush()

    def record_permission_profile(self, profile: str, *, phase: str = "") -> None:
        value = _identifier(profile, 80)
        if not value:
            return
        self.manifest.permission_profile = value
        item = {"profile": value}
        phase_text = _identifier(phase, 80)
        if phase_text:
            item["phase"] = phase_text
        if item not in self.manifest.permission_profiles:
            self.manifest.permission_profiles.append(item)
            if len(self.manifest.permission_profiles) > MAX_PERMISSION_PROFILES:
                del self.manifest.permission_profiles[:-MAX_PERMISSION_PROFILES]
                self.manifest.warnings.append("permission_profiles_truncated")
        self.checkpoint()

    def record_tool_contract_hash(self, value: object, *, phase: str = "") -> None:
        text = _clip(value, 80)
        if text:
            self.manifest.model_tool_contract_hash = text
            item = {"hash": text}
            phase_text = _identifier(phase, 80)
            if phase_text:
                item["phase"] = phase_text
            if item not in self.manifest.tool_contracts:
                self.manifest.tool_contracts.append(item)
                if len(self.manifest.tool_contracts) > MAX_TOOL_CONTRACTS:
                    del self.manifest.tool_contracts[:-MAX_TOOL_CONTRACTS]
                    self.manifest.warnings.append("tool_contracts_truncated")
            self.checkpoint()

    def record_runtime_tool_contract_hash(self, value: object, *, phase: str = "") -> None:
        text = _clip(value, 80)
        if text:
            self.manifest.runtime_tool_contract_hash = text
            item = {"hash": text, "surface": "runtime"}
            phase_text = _identifier(phase, 80)
            if phase_text:
                item["phase"] = phase_text
            if item not in self.manifest.tool_contracts:
                self.manifest.tool_contracts.append(item)
                if len(self.manifest.tool_contracts) > MAX_TOOL_CONTRACTS:
                    del self.manifest.tool_contracts[:-MAX_TOOL_CONTRACTS]
                    self.manifest.warnings.append("tool_contracts_truncated")
            self.checkpoint()

    def record_prompt_section(
        self,
        name: str,
        text: object,
        *,
        purpose: str = "",
        model_visible: bool = True,
        budget: int = 0,
        truncated: bool = False,
        freshness: str = "",
        source_refs: Iterable[object] = (),
        epoch_id: str = "",
        admission_reason: str = "",
        capability_id: str = "",
    ) -> None:
        rendered = str(text or "")
        if not rendered:
            return
        refs = _bounded_refs(source_refs)
        if not refs and model_visible:
            fallback = _identifier(name, 80) or "prompt_section"
            refs = (f"prompt_section:{fallback}",)
        model_boundary = is_model_boundary_freshness(freshness)
        item = PromptSectionTrace(
            name=name,
            digest=digest_text(rendered),
            chars=len(rendered),
            purpose=str(purpose or ""),
            model_visible=bool(model_visible),
            budget=max(0, int(budget or 0)),
            truncated=bool(truncated),
            freshness=freshness,
            source_refs=refs,
            epoch_id=_identifier(epoch_id, 80),
            admission_reason=_identifier(admission_reason, 80),
            capability_id=_identifier(capability_id, 80),
        )
        key = (
            item.name,
            item.digest,
            item.purpose,
            item.freshness,
            refs,
            item.model_visible,
            item.epoch_id,
        )
        if key in self._prompt_keys:
            if model_boundary:
                self.flush()
            return
        self._prompt_keys.add(key)
        self.manifest.prompt_sections.append(item)
        if len(self.manifest.prompt_sections) > MAX_PROMPT_SECTIONS:
            del self.manifest.prompt_sections[:-MAX_PROMPT_SECTIONS]
            self.manifest.warnings.append("prompt_sections_truncated")
        if model_boundary:
            self.flush()
        else:
            self.checkpoint()

    def record_context_sources(
        self,
        sources: Iterable[Any],
        *,
        epoch_id: str = "",
        admission_reason: str = "",
    ) -> None:
        """Record rendered context sources as bounded prompt-section rows.

        Rows are projected through the shared ContextEpoch admission
        projection, so trace rows and snapshots share one ref/digest
        vocabulary. When epoch_id is supplied (the outbound prompt's
        content-addressed epoch), each row is bound to that provider turn;
        the per-source admission_reason wins over the caller's fallback.
        """
        normalized_epoch = _identifier(epoch_id, 80)
        changed = False
        for source in sources:
            admission = admission_from_rendered_source(
                source,
                admission_reason=admission_reason,
            )
            if admission is None:
                continue
            item = PromptSectionTrace(
                name=admission.source_key,
                digest=admission.digest,
                chars=admission.chars,
                purpose=str(getattr(source, "why_included", "") or ""),
                model_visible=True,
                budget=admission.budget,
                truncated=admission.truncated,
                freshness=str(getattr(source, "freshness", "") or ""),
                source_refs=(admission.source_ref,),
                epoch_id=normalized_epoch,
                admission_reason=admission.admission_reason,
                capability_id=admission.capability_id,
            )
            key = (
                item.name,
                item.digest,
                item.purpose,
                item.freshness,
                item.source_refs,
                item.model_visible,
                item.epoch_id,
            )
            if key in self._prompt_keys:
                continue
            self._prompt_keys.add(key)
            self.manifest.prompt_sections.append(item)
            changed = True
        if changed:
            if len(self.manifest.prompt_sections) > MAX_PROMPT_SECTIONS:
                del self.manifest.prompt_sections[:-MAX_PROMPT_SECTIONS]
                self.manifest.warnings.append("prompt_sections_truncated")
            self.checkpoint()

    def record_local_context_refs(self, refs: Iterable[Mapping[str, object]]) -> None:
        changed = False
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            item_id = _clip(ref.get("id"), 120)
            scope = _identifier(ref.get("scope"), 40)
            kind = _identifier(ref.get("kind"), 80)
            if not item_id:
                continue
            key = (item_id, scope, kind)
            if key in self._local_context_keys:
                continue
            self._local_context_keys.add(key)
            payload: dict[str, object] = {"id": item_id}
            if scope:
                payload["scope"] = scope
            if kind:
                payload["kind"] = kind
            source = _identifier(ref.get("source"), 80)
            if source:
                payload["source"] = source
            self.manifest.local_context_refs.append(payload)
            changed = True
        if changed:
            if len(self.manifest.local_context_refs) > MAX_REFS:
                del self.manifest.local_context_refs[:-MAX_REFS]
                self.manifest.warnings.append("local_context_refs_truncated")
            self.checkpoint()

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

    def record_research_sources(self, sources: Iterable[Mapping[str, object]]) -> None:
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
        if not isinstance(summary, Mapping):
            return
        record_id = _generated_ref(summary.get("record_id"), "research_record")
        digest = valid_digest_ref(summary.get("record_digest"))
        if not record_id or not digest or record_id in self._research_record_keys:
            return
        answer_status = _research_answer_status(summary.get("answer_status"))
        payload: dict[str, object] = {
            "record_id": record_id,
            "answer_status": answer_status,
            "source_count": _nonnegative_int(summary.get("source_count")),
            "evidence_count": _nonnegative_int(summary.get("evidence_count")),
            "claim_count": _nonnegative_int(summary.get("claim_count")),
            "assumption_count": _nonnegative_int(summary.get("assumption_count")),
            "unsupported_claim_count": _nonnegative_int(summary.get("unsupported_claim_count")),
            "record_digest": digest,
        }
        self._research_record_keys.add(record_id)
        self.manifest.research_records.append(payload)
        if len(self.manifest.research_records) > MAX_RESEARCH_RECORDS:
            del self.manifest.research_records[:-MAX_RESEARCH_RECORDS]
            self.manifest.warnings.append("research_records_truncated")
        self.checkpoint()

    def record_evidence_ledger_write(self, result: Mapping[str, object]) -> None:
        if not isinstance(result, Mapping):
            return
        record_id = _generated_ref(result.get("record_id"), "research_record")
        ledger_ref = _generated_ref(result.get("ledger_ref"), "evidence_ledger")
        if not record_id:
            return
        counts = result.get("counts")
        payload: dict[str, object] = {
            "ok": bool(result.get("ok")),
            "skipped": bool(result.get("skipped")),
            "reason_code": _safe_trace_code(result.get("reason_code"), 80),
            "ledger_ref": ledger_ref,
            "record_id": record_id,
            "counts": _bounded_count_mapping(counts if isinstance(counts, Mapping) else {}),
        }
        warnings = [
            _safe_trace_code(item, 120)
            for item in _trace_list_items(result.get("warnings"))
            if _safe_trace_code(item, 120)
        ][:MAX_WARNINGS]
        if warnings:
            payload["warnings"] = warnings
        self.manifest.research_evidence_ledgers.append(payload)
        if len(self.manifest.research_evidence_ledgers) > MAX_EVIDENCE_LEDGER_WRITES:
            del self.manifest.research_evidence_ledgers[:-MAX_EVIDENCE_LEDGER_WRITES]
            self.manifest.warnings.append("research_evidence_ledgers_truncated")
        self.checkpoint()

    def record_research_proof_review(self, review: Mapping[str, object]) -> None:
        if not isinstance(review, Mapping):
            return
        proof_ref = _generated_ref(review.get("proof_ref"), "research_proof")
        if not proof_ref:
            return
        question_digest = valid_digest_ref(review.get("question_digest"))
        payload: dict[str, object] = {
            "proof_ref": proof_ref,
            "ok": bool(review.get("ok")),
            "answers_question": bool(review.get("answers_question")),
            "answer_status": _research_answer_status(review.get("answer_status")),
            "answer_coverage_score": _unit_float(review.get("answer_coverage_score")),
            "gap_count": _nonnegative_int(review.get("gap_count")),
            "warning_count": _nonnegative_int(review.get("warning_count")),
            "planner_signal_count": _nonnegative_int(review.get("planner_signal_count")),
            "reason_codes": [
                _safe_trace_code(item, 80)
                for item in _trace_list_items(review.get("reason_codes"))
                if _safe_trace_code(item, 80)
            ][:MAX_WARNINGS],
        }
        record_id = _generated_ref(review.get("record_id"), "research_record")
        if record_id:
            payload["record_id"] = record_id
        digest = valid_digest_ref(review.get("record_digest"))
        if digest:
            payload["record_digest"] = digest
        if payload["ok"] and (not record_id or not digest):
            return
        if question_digest:
            payload["question_digest"] = question_digest
        review_key = (
            str(payload["proof_ref"]),
            str(payload.get("question_digest") or ""),
            tuple(payload["reason_codes"]),
        )
        for existing in self.manifest.research_proof_reviews:
            existing_key = (
                str(existing.get("proof_ref") or ""),
                str(existing.get("question_digest") or ""),
                tuple(existing.get("reason_codes", ()) or ()),
            )
            if existing_key == review_key:
                return
        self.manifest.research_proof_reviews.append(payload)
        if len(self.manifest.research_proof_reviews) > MAX_RESEARCH_PROOF_REVIEWS:
            del self.manifest.research_proof_reviews[:-MAX_RESEARCH_PROOF_REVIEWS]
            self.manifest.warnings.append("research_proof_reviews_truncated")
        self.checkpoint()

    def record_research_plan(self, plan: Mapping[str, object]) -> None:
        if not isinstance(plan, Mapping):
            return
        plan_ref = _generated_ref(plan.get("plan_ref"), "research_plan")
        if not plan_ref or plan_ref in self._research_plan_keys:
            return
        question_digest = valid_digest_ref(plan.get("question_digest"))
        proof_ref = _generated_ref(plan.get("proof_ref"), "research_proof")
        preferences: list[str] = []
        for item in _trace_list_items(plan.get("source_preferences")):
            connector_id = _safe_connector_id(item)
            if connector_id:
                preferences.append(connector_id)
            if len(preferences) >= MAX_REFS:
                break
        payload: dict[str, object] = {
            "plan_ref": plan_ref,
            "dry_run": True,
            "max_depth": _bounded_int(plan.get("max_depth"), 1, 1),
            "max_queries": _bounded_int(plan.get("max_queries"), 1, 8),
            "max_sources": _bounded_int(plan.get("max_sources"), 1, 12),
            "query_count": _bounded_int(plan.get("query_count"), 0, 8),
            "source_preferences": preferences,
            "reason_codes": [
                _safe_trace_code(item, 80)
                for item in _trace_list_items(plan.get("reason_codes"))
                if _safe_trace_code(item, 80)
            ][:MAX_WARNINGS],
            "warnings": [
                _safe_trace_code(item, 120)
                for item in _trace_list_items(plan.get("warnings"))
                if _safe_trace_code(item, 120)
            ][:MAX_WARNINGS],
        }
        if question_digest:
            payload["question_digest"] = question_digest
        if proof_ref:
            payload["proof_ref"] = proof_ref
        self._research_plan_keys.add(plan_ref)
        self.manifest.research_plans.append(payload)
        if len(self.manifest.research_plans) > MAX_RESEARCH_PLANS:
            del self.manifest.research_plans[:-MAX_RESEARCH_PLANS]
            self.manifest.warnings.append("research_plans_truncated")
        self.checkpoint()

    def record_research_pipeline_result(self, result: Mapping[str, object]) -> None:
        if not isinstance(result, Mapping):
            return
        payload = {
            "followup_applied": bool(result.get("followup_applied")),
            "followup_rounds": _bounded_int(result.get("followup_rounds"), 0, 3),
            "stop_reason": _safe_trace_code(result.get("stop_reason"), 80),
            "planner_stop_reason": _safe_trace_code(result.get("planner_stop_reason"), 80),
            "fresh_source_count": _nonnegative_int(result.get("fresh_source_count")),
            "new_evidence_count": _nonnegative_int(result.get("new_evidence_count")),
            "final_evidence_count": _nonnegative_int(result.get("final_evidence_count")),
            "attempted_fresh_source_count": _nonnegative_int(result.get("attempted_fresh_source_count")),
            "attempted_new_evidence_count": _nonnegative_int(result.get("attempted_new_evidence_count")),
        }
        self.manifest.research_pipeline_runs.append(payload)

        if len(self.manifest.research_pipeline_runs) > MAX_RESEARCH_PIPELINE_RUNS:
            del self.manifest.research_pipeline_runs[:-MAX_RESEARCH_PIPELINE_RUNS]
            self.manifest.warnings.append("research_pipeline_runs_truncated")
        self.checkpoint()

    def record_research_connector_errors(self, errors: Iterable[object]) -> None:
        if not isinstance(errors, Iterable):
            return
        counts: dict[tuple[str, str, str], int] = {}
        for item in errors:
            if not isinstance(item, Mapping):
                continue
            connector_id = _safe_connector_id(item.get("connector_id"))
            action = _safe_trace_code(item.get("action"), 80)
            error = _safe_trace_code(item.get("error"), 120)
            count = _nonnegative_int(item.get("count")) or 1
            if not connector_id or not action or not error:
                continue
            key = (connector_id, action, error)
            counts[key] = min(999, counts.get(key, 0) + count)
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
            payload = {
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
        if not isinstance(result, Mapping):
            return
        reason = _safe_trace_code(result.get("reason"), 80)
        if not reason:
            return
        payload = {
            "reason": reason,
            "source_count": _bounded_int(result.get("source_count"), 0, 64),
        }
        self.manifest.research_done_compilations.append(payload)
        if len(self.manifest.research_done_compilations) > MAX_RESEARCH_DONE_COMPILATIONS:
            del self.manifest.research_done_compilations[:-MAX_RESEARCH_DONE_COMPILATIONS]
            self.manifest.warnings.append("research_done_compilations_truncated")
        self.checkpoint()

    def record_analysis_run(self, record: Mapping[str, object]) -> None:
        if not isinstance(record, Mapping):
            return
        ref = _generated_ref(record.get("analysis_run_id"), "analysis_run")
        tool_id = _tool_instance_id(record.get("tool_id"))
        tool_name = _identifier(record.get("tool_name"), 40)
        if not ref or ref in self._analysis_run_keys or not tool_id or not tool_name:
            return
        cwd_ref = record.get("cwd_ref")
        command_display, command_display_redacted = _analysis_command_display(record.get("command_display"))
        warnings = [
            _safe_trace_code(item, 120)
            for item in _trace_list_items(record.get("warnings"))
            if _safe_trace_code(item, 120)
        ][:MAX_WARNINGS]
        if command_display_redacted and "command_display_redacted" not in warnings:
            if len(warnings) >= MAX_WARNINGS:
                warnings = warnings[: MAX_WARNINGS - 1]
            warnings.append("command_display_redacted")
        payload: dict[str, object] = {
            "analysis_run_id": ref,
            "run_id": str(record.get("run_id") or "")[:120],
            "tool_id": tool_id,
            "tool_name": tool_name,
            "command_digest": valid_digest_ref(record.get("command_digest")),
            "command_display": command_display,
            "cwd_ref": dict(cwd_ref) if isinstance(cwd_ref, Mapping) else {},
            "exit_code": _int_or_none(record.get("exit_code")),
            "ok": bool(record.get("ok")),
            "started_at": str(record.get("started_at") or "")[:40],
            "finished_at": str(record.get("finished_at") or "")[:40],
            "duration_ms": _int_or_none(record.get("duration_ms")),
            "managed_output_handle": str(record.get("managed_output_handle") or "")[:80],
            "output_sha256": valid_digest_ref(
                f"sha256:{record.get('output_sha256')}"
                if str(record.get("output_sha256") or "")
                else ""
            ),
            "stored_truncated": bool(record.get("stored_truncated")),
            "capture_quality": _safe_trace_code(record.get("capture_quality"), 40),
            "reproduction_status": _safe_trace_code(record.get("reproduction_status"), 40),
            "environment_digest": valid_digest_ref(record.get("environment_digest")),
            "warnings": warnings[:MAX_WARNINGS],
        }
        self._analysis_run_keys.add(ref)
        self.manifest.analysis_runs.append(payload)
        if len(self.manifest.analysis_runs) > MAX_ANALYSIS_RUNS:
            del self.manifest.analysis_runs[:-MAX_ANALYSIS_RUNS]
            self.manifest.warnings.append("analysis_runs_truncated")
        self.checkpoint()

    def record_artifact_refs(self, refs: Iterable[object]) -> None:
        for item in _trace_list_items(refs):
            if not isinstance(item, Mapping):
                continue
            version_id = _generated_ref(item.get("version_id"), "artifact_version")
            artifact_id = _generated_ref(item.get("artifact_id"), "artifact")
            if not artifact_id or not version_id or version_id in self._artifact_version_keys:
                continue
            derived = [
                str(ref or "")[:120]
                for ref in _trace_list_items(item.get("derived_from"))
                if is_valid_derived_ref(ref)
            ][:8]
            payload: dict[str, object] = {
                "artifact_id": artifact_id,
                "version_id": version_id,
                "artifact_kind": _identifier(item.get("artifact_kind"), 40),
                "sha256": valid_digest_ref(
                    f"sha256:{item.get('sha256')}"
                    if str(item.get("sha256") or "")
                    else ""
                ),
                "size": _nonnegative_int(item.get("size")),
                "mime": str(item.get("mime") or "")[:80],
                "origin_run_id": str(item.get("origin_run_id") or "")[:120],
                "produced_by": str(item.get("produced_by") or "")[:120],
                "stored_truncated": bool(item.get("stored_truncated")),
                "derived_from": derived,
                "warnings": [
                    _safe_trace_code(warning, 120)
                    for warning in _trace_list_items(item.get("warnings"))
                    if _safe_trace_code(warning, 120)
                ][:4],
            }
            self._artifact_version_keys.add(version_id)
            self.manifest.artifact_refs.append(payload)
        if len(self.manifest.artifact_refs) > MAX_ARTIFACT_REFS:
            del self.manifest.artifact_refs[:-MAX_ARTIFACT_REFS]
            self.manifest.warnings.append("artifact_refs_truncated")
        self.checkpoint()

    def record_reproducibility_capsule(self, capsule: Mapping[str, object]) -> None:
        if not isinstance(capsule, Mapping):
            return
        ref = _generated_ref(capsule.get("capsule_id"), "capsule")
        if not ref:
            return
        payload: dict[str, object] = {
            "capsule_id": ref,
            "run_id": str(capsule.get("run_id") or "")[:120],
            "analysis_run_refs": [
                analysis_ref
                for analysis_ref in (
                    _generated_ref(item, "analysis_run")
                    for item in _trace_list_items(capsule.get("analysis_run_refs"))
                )
                if analysis_ref
            ][:MAX_ANALYSIS_RUNS],
            "artifact_refs": [
                version_ref
                for version_ref in (
                    _generated_ref(item, "artifact_version")
                    for item in _trace_list_items(capsule.get("artifact_refs"))
                )
                if version_ref
            ][:MAX_CAPSULE_ARTIFACT_REFS],
            "environment_digest": valid_digest_ref(capsule.get("environment_digest")),
            "reproduction_status": _safe_trace_code(
                capsule.get("reproduction_status"), 40
            ),
            "warnings": [
                _safe_trace_code(item, 120)
                for item in _trace_list_items(capsule.get("warnings"))
                if _safe_trace_code(item, 120)
            ][:MAX_WARNINGS],
        }
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
            raw = item.to_payload() if callable(getattr(item, "to_payload", None)) else item
            if not isinstance(raw, Mapping):
                continue
            finding_id = _generated_ref(raw.get("finding_id"), "review_finding")
            kind = _safe_trace_code(raw.get("kind"), 40)
            if not finding_id or kind not in FINDING_KINDS or finding_id in self._review_finding_keys:
                continue
            severity = _safe_trace_code(raw.get("severity"), 20)
            status = _safe_trace_code(raw.get("status"), 20)
            payload: dict[str, object] = {
                "finding_id": finding_id,
                "kind": kind,
                "severity": severity if severity in FINDING_SEVERITIES else SEVERITY_WARNING,
                "status": status if status in FINDING_STATUSES else STATUS_OPEN,
                "target_ref": _normalize_runtime_ref(raw.get("target_ref")),
                "reason_codes": [
                    code
                    for code in (
                        _safe_trace_code(value, 80)
                        for value in _trace_list_items(raw.get("reason_codes"))
                    )
                    if code
                ][:MAX_WARNINGS],
            }
            for key, ref_kind in REVIEW_FINDING_REF_KINDS.items():
                ref = _normalize_runtime_ref(raw.get(key), kind=ref_kind)
                if ref:
                    payload[key] = ref
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
            raw = item.to_payload() if callable(getattr(item, "to_payload", None)) else item
            if not isinstance(raw, Mapping):
                continue
            gap_id = _generated_ref(raw.get("gap_id"), "planner_gap")
            gap_kind = _safe_trace_code(raw.get("gap_kind"), 40)
            if not gap_id or gap_kind not in GAP_KINDS or gap_id in self._planner_gap_keys:
                continue
            payload = {
                "gap_id": gap_id,
                "gap_kind": gap_kind,
                "target_ref": _normalize_runtime_ref(raw.get("target_ref")),
                "reason_codes": [
                    code
                    for code in (
                        _safe_trace_code(value, 80)
                        for value in _trace_list_items(raw.get("reason_codes"))
                    )
                    if code
                ][:MAX_WARNINGS],
                "finding_refs": [
                    ref
                    for ref in (
                        _normalize_runtime_ref(value, kind="review_finding")
                        for value in _trace_list_items(raw.get("finding_refs"))
                    )
                    if ref
                ][:MAX_GAP_FINDING_REFS],
            }
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
            raw = item.to_payload() if callable(getattr(item, "to_payload", None)) else item
            if not isinstance(raw, Mapping):
                continue
            source_ref = _normalize_runtime_ref(raw.get("source_ref"), kind="source")
            source_class = _safe_trace_code(raw.get("source_class"), 40)
            if (
                not source_ref
                or source_class not in _SOURCE_TRUST_CLASSES
                or source_ref in self._source_trust_keys
            ):
                continue
            payload: dict[str, object] = {
                "source_ref": source_ref,
                "source_class": source_class,
                "tier": _bounded_int(raw.get("tier"), 1, 3),
                "freshness": _safe_trace_code(raw.get("freshness"), 20) or "undated",
                "host": _clip(raw.get("host"), 120),
                "classes": [
                    cls
                    for cls in (
                        _safe_trace_code(value, 40)
                        for value in _trace_list_items(raw.get("classes"))
                    )
                    if cls in _SOURCE_TRUST_CLASSES
                ][:MAX_SOURCE_TRUST_CLASSES],
                "warnings": [
                    code
                    for code in (
                        _safe_trace_code(value, 80)
                        for value in _trace_list_items(raw.get("warnings"))
                    )
                    if code
                ][:MAX_WARNINGS],
            }
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

        if not isinstance(projection, Mapping):
            return
        record_ref = _normalize_runtime_ref(projection.get("record_ref"), kind="research_record")
        digest = valid_digest_ref(projection.get("record_digest"))
        profile_id = _safe_trace_code(projection.get("profile_id"), 80)
        if not record_ref or not digest:
            return
        key = (record_ref, digest)
        if key in self._brief_projection_keys:
            return
        answer_status = _research_answer_status(projection.get("answer_status"))
        payload: dict[str, object] = {
            "record_ref": record_ref,
            "record_digest": digest,
            "answer_status": answer_status,
            "profile_id": profile_id,
            "claim_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="claim")
                    for value in _trace_list_items(projection.get("claim_refs"))
                )
                if ref
            ][:MAX_BRIEF_REFS],
            "evidence_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="evidence")
                    for value in _trace_list_items(projection.get("evidence_refs"))
                )
                if ref
            ][:MAX_BRIEF_REFS],
            "assumption_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="assumption")
                    for value in _trace_list_items(projection.get("assumption_refs"))
                )
                if ref
            ][:MAX_BRIEF_REFS],
            "analysis_run_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="analysis_run")
                    for value in _trace_list_items(projection.get("analysis_run_refs"))
                )
                if ref
            ][:MAX_ANALYSIS_RUNS],
            "artifact_version_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="artifact_version")
                    for value in _trace_list_items(projection.get("artifact_version_refs"))
                )
                if ref
            ][:MAX_CAPSULE_ARTIFACT_REFS],
            "proof_review_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="research_proof")
                    for value in _trace_list_items(projection.get("proof_review_refs"))
                )
                if ref
            ][:MAX_BRIEF_PROJECTIONS],
            "planner_gap_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="planner_gap")
                    for value in _trace_list_items(projection.get("planner_gap_refs"))
                )
                if ref
            ][:MAX_BRIEF_PROJECTIONS],
            "review_finding_refs": [
                ref
                for ref in (
                    _normalize_runtime_ref(value, kind="review_finding")
                    for value in _trace_list_items(projection.get("review_finding_refs"))
                )
                if ref
            ][:MAX_BRIEF_PROJECTIONS],
            "contract_refs": [
                code
                for code in (
                    _safe_trace_code(value, 80)
                    for value in _trace_list_items(projection.get("contract_refs"))
                )
                if code
            ][:MAX_WARNINGS],
            "warnings": [
                code
                for code in (
                    _safe_trace_code(value, 80)
                    for value in _trace_list_items(projection.get("warnings"))
                )
                if code
            ][:MAX_WARNINGS],
        }
        claim_rows: list[dict[str, object]] = []
        claims_raw = projection.get("claims")
        for row in _trace_list_items(claims_raw):
            if not isinstance(row, Mapping) or len(claim_rows) >= MAX_BRIEF_CLAIM_ROWS:
                continue
            claim_ref = _normalize_runtime_ref(row.get("claim_ref"), kind="claim")
            status = _safe_trace_code(row.get("status"), 20)
            text = str(row.get("text") or "").strip()
            if not text or status not in {"evidence_backed", "assumption", "unsupported"}:
                continue
            # The trace is not a transcript: claim prose stays out, the text
            # travels only as a digest resolvable against the research
            # record's own bounded payloads. Clip first so even a malformed
            # direct mapping cannot push unbounded bytes through the hash.
            entry: dict[str, object] = {
                "status": status,
                "evidence_count": _nonnegative_int(row.get("evidence_count")),
                "text_digest": digest_text(_clip(text, 260)),
            }
            if claim_ref:
                entry["claim_ref"] = claim_ref
            claim_rows.append(entry)
        if not claim_rows and not payload["claim_refs"]:
            return
        payload["claims"] = claim_rows
        counts = projection.get("counts")
        if isinstance(counts, Mapping):
            payload["counts"] = _bounded_count_mapping(counts)
        self._brief_projection_keys.add(key)
        self.manifest.research_brief_projections.append(payload)
        if len(self.manifest.research_brief_projections) > MAX_BRIEF_PROJECTIONS:
            del self.manifest.research_brief_projections[:-MAX_BRIEF_PROJECTIONS]
            self.manifest.warnings.append("research_brief_projections_truncated")
        self.checkpoint()

    def record_completion_proof(self, proof: Any) -> None:
        """Record one bounded completion proof (refs and statuses only)."""

        raw = proof.to_payload() if callable(getattr(proof, "to_payload", None)) else proof
        if not isinstance(raw, Mapping):
            return
        proof_id = _generated_ref(raw.get("proof_id"), "completion_proof")
        contract_id = _generated_ref(raw.get("contract_id"), "completion_contract")
        domain = _safe_trace_code(raw.get("domain"), 20)
        status = _safe_trace_code(raw.get("status"), 40)
        if (
            not proof_id
            or not contract_id
            or proof_id in self._completion_proof_keys
            or domain not in _COMPLETION_TRACE_DOMAINS
            or status not in _COMPLETION_TRACE_STATUSES
        ):
            return
        satisfied = status in _COMPLETION_SATISFIED_STATUSES
        payload: dict[str, object] = {
            "proof_id": proof_id,
            "contract_id": contract_id,
            "domain": domain,
            "status": status,
            "satisfied": satisfied,
        }
        check_rows: list[dict[str, object]] = []
        for item in _trace_list_items(raw.get("checks")):
            if not isinstance(item, Mapping) or len(check_rows) >= MAX_COMPLETION_CHECK_ROWS:
                continue
            check_id = _safe_trace_code(item.get("check_id"), 80)
            check_status = _safe_trace_code(item.get("status"), 20)
            if not check_id or check_status not in _COMPLETION_CHECK_STATUSES:
                continue
            row: dict[str, object] = {"check_id": check_id, "status": check_status}
            reason_code = _safe_trace_code(item.get("reason_code"), 120)
            if reason_code:
                row["reason_code"] = reason_code
            check_rows.append(row)
        if not check_rows:
            return
        ref_groups: dict[str, list[str]] = {}
        for key in ("evidence_refs", "limitation_refs", "external_refs"):
            refs = [
                _safe_trace_code(value, 160)
                for value in _trace_list_items(raw.get(key))
            ]
            ref_groups[key] = [ref for ref in refs if ref][:MAX_GAP_FINDING_REFS]
        if (
            status == _COMPLETION_COMPLETE_WITH_LIMITATIONS
            and not ref_groups["limitation_refs"]
        ):
            return
        payload["checks"] = check_rows
        blocked_reason = _safe_trace_code(raw.get("blocked_reason"), 120)
        if blocked_reason and not satisfied:
            payload["blocked_reason"] = blocked_reason
        reason_codes = [
            code
            for code in (
                _safe_trace_code(value, 80)
                for value in _trace_list_items(raw.get("reason_codes"))
            )
            if code
        ][:MAX_WARNINGS]
        if reason_codes:
            payload["reason_codes"] = reason_codes
        # subject_ref is an opaque bounded token (run:/ledger:/research:...),
        # not necessarily a runtime ref kind, so it gets code sanitation
        # instead of ref-kind validation.
        subject_ref = _safe_trace_code(raw.get("subject_ref"), 160)
        if subject_ref:
            payload["subject_ref"] = subject_ref
        payload["finding_refs"] = [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="review_finding")
                for value in _trace_list_items(raw.get("finding_refs"))
            )
            if ref
        ][:MAX_GAP_FINDING_REFS]
        payload["analysis_run_refs"] = [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="analysis_run")
                for value in _trace_list_items(raw.get("analysis_run_refs"))
            )
            if ref
        ][:MAX_ANALYSIS_RUNS]
        payload["artifact_refs"] = [
            ref
            for ref in (
                _normalize_runtime_ref(value, kind="artifact_version")
                for value in _trace_list_items(raw.get("artifact_refs"))
            )
            if ref
        ][:MAX_CAPSULE_ARTIFACT_REFS]
        payload.update(ref_groups)
        self._completion_proof_keys.add(proof_id)
        self.manifest.completion_proofs.append(payload)
        if len(self.manifest.completion_proofs) > MAX_COMPLETION_PROOFS:
            del self.manifest.completion_proofs[:-MAX_COMPLETION_PROOFS]
            self.manifest.warnings.append("completion_proofs_truncated")
        self.checkpoint()

    def record_fallback(
        self,
        *,
        from_provider: str,
        to_provider: str,
        phase: str,
        reason_code: str,
    ) -> None:
        self.manifest.fallbacks.append(FallbackTrace(
            from_provider=from_provider,
            to_provider=to_provider,
            phase=phase,
            reason_code=reason_code,
        ))
        if len(self.manifest.fallbacks) > MAX_FALLBACKS:
            del self.manifest.fallbacks[:-MAX_FALLBACKS]
            self.manifest.warnings.append("fallbacks_truncated")
        if to_provider:
            self.manifest.provider_final = _identifier(to_provider, 80)
        self.flush()

    def record_provider_failure(self, provider: str, failure: Any) -> None:
        payload = {
            "provider": _identifier(provider, 80),
            "action": _identifier(getattr(failure, "action", ""), 80),
            "kind": _identifier(getattr(failure, "kind", ""), 120),
            "stage": _identifier(getattr(failure, "stage", ""), 120),
        }
        if not any(payload.values()):
            return
        self.manifest.provider_failures.append(payload)
        if len(self.manifest.provider_failures) > MAX_FAILURES:
            del self.manifest.provider_failures[:-MAX_FAILURES]
            self.manifest.warnings.append("provider_failures_truncated")
        self.flush()

    def record_policy_decision(self, decision: Any) -> None:
        if hasattr(decision, "to_audit_payload"):
            raw = decision.to_audit_payload()
        elif isinstance(decision, Mapping):
            raw = decision
        else:
            return
        subject_ref = _action_ref_or_empty(raw.get("subject_ref"))
        payload = {
            "kind": _identifier(raw.get("kind"), 80),
            "decision": _identifier(raw.get("decision"), 40),
            "guard_id": _identifier(raw.get("guard_id"), 80),
            "reason_code": _identifier(raw.get("reason_code"), 120),
            "phase": _identifier(raw.get("phase"), 80),
            "subject_ref": subject_ref,
        }
        display_digest = valid_digest_ref(raw.get("display_digest"))
        if display_digest:
            payload["display_digest"] = display_digest
        display_chars = _int_or_none(raw.get("display_chars"))
        if display_chars is not None:
            payload["display_chars"] = display_chars
        if (
            not payload["kind"]
            or payload["decision"] not in {"allow", "ask_user", "deny"}
            or not payload["subject_ref"]
        ):
            return
        key = (
            str(payload["kind"]),
            str(payload["decision"]),
            str(payload["guard_id"]),
            str(payload["reason_code"]),
            str(payload["subject_ref"]),
        )
        if key in self._policy_keys:
            return
        self._policy_keys.add(key)
        self.manifest.policy_decisions.append(payload)
        if len(self.manifest.policy_decisions) > MAX_POLICY_DECISIONS:
            del self.manifest.policy_decisions[:-MAX_POLICY_DECISIONS]
            self.manifest.warnings.append("policy_decisions_truncated")
        self.checkpoint()

    def finish(self, *, status: str, mode: str = "", provider: str = "") -> None:
        self.manifest.status = _identifier(status, 40) or "done"
        if mode:
            self.manifest.mode_final = _identifier(mode, 40)
        if provider:
            self.manifest.provider_final = _identifier(provider, 80)
        self.flush()

    def warn(self, reason_code: str) -> None:
        reason = _identifier(reason_code, 120)
        if reason:
            self.manifest.warnings.append(reason)
            self.flush()

    def flush(self) -> None:
        if self.disabled:
            return
        try:
            write_json_atomic(
                self.path,
                self.manifest.to_payload(),
                max_bytes=MAX_TRACE_BYTES,
            )
            self._dirty_updates = 0
        except (OSError, TypeError, ValueError):
            self.disabled = True

    def checkpoint(self) -> None:
        if self.disabled:
            return
        self._dirty_updates += 1
        if self._dirty_updates >= CHECKPOINT_FLUSH_INTERVAL:
            self.flush()


def _host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _action_ref_or_empty(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("action:") and _is_hex_64(text.removeprefix("action:")):
        return text
    return ""


def _research_connector_error_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    connector_id = _safe_connector_id(value.get("connector_id"))
    action = _safe_trace_code(value.get("action"), 80)
    error = _safe_trace_code(value.get("error"), 120)
    count = _nonnegative_int(value.get("count")) or 1
    if not connector_id or not action or not error:
        return {}
    return {
        "connector_id": connector_id,
        "action": action,
        "error": error,
        "count": min(999, count),
    }


def _safe_trace_code(value: object, limit: int) -> str:
    raw = _clip(value, limit)
    if not raw:
        return ""
    if looks_sensitive_code(raw):
        return ""
    text = _identifier(raw, limit)
    if looks_sensitive_code(text):
        return ""
    return text


def _analysis_command_display(value: object) -> tuple[str, bool]:
    text = _clip(value, 500)
    if not text:
        return "", False
    if looks_sensitive_signal(text):
        return "", True
    return text, False


def _trace_list_items(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value, key=str))
    return ()


def _bounded_count_mapping(value: Mapping[str, object]) -> dict[str, int]:
    allowed = {
        "records",
        "sources",
        "evidence",
        "claims",
        "assumptions",
        "relations",
    }
    return {
        key: _nonnegative_int(raw)
        for key, raw in value.items()
        if isinstance(key, str) and key in allowed
    }


def _research_answer_status(value: object) -> str:
    text = _identifier(value, 40)
    return text if text in RESEARCH_ANSWER_STATUSES else "not_answered"


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _bounded_int(value: object, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        return lower
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = lower
    return max(lower, min(upper, parsed))


def _unit_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return round(number, 3)


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _clip(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX


def _identifier(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = _clip(value, limit)
    return "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)


def _tool_instance_id(value: object) -> str:
    text = _identifier(value, 40)
    turn, sep, index = text.partition(":")
    return text if sep and turn.isdigit() and index.isdigit() else ""


def _bounded_refs(values: Iterable[object], *, limit: int = MAX_REFS) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    refs: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = _clip(value, 160)
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= limit:
            break
    return tuple(refs)


def _safe_file_stem(value: object) -> str:
    text = _clip(value, 120)
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in text)
    return safe.strip("._") or "run"
