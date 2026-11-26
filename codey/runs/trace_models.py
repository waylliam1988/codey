"""Bounded trace data models and their serialized manifest payload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_protocol import _protocol_telemetry_payload
from codey.runs.trace_research import _research_connector_error_payload
from codey.runs.trace_schema import (
    MAX_ANALYSIS_RUNS,
    MAX_ARTIFACT_REFS,
    MAX_BRIEF_PROJECTIONS,
    MAX_COMPLETION_PROOFS,
    MAX_COMPLETION_REPAIR_ROWS,
    MAX_EDIT_INTEGRITY_ROWS,
    MAX_EVIDENCE_LEDGER_WRITES,
    MAX_FAILURES,
    MAX_FALLBACKS,
    MAX_PERMISSION_PROFILES,
    MAX_PLANNER_GAPS,
    MAX_POLICY_DECISIONS,
    MAX_PROMPT_SECTIONS,
    MAX_PROMPT_SURFACES,
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
    MAX_TOOL_CONTRACTS,
    MAX_TOPIC_CONTINUITY_ROWS,
    MAX_WARNINGS,
    SCHEMA_VERSION,
    TRACE_KIND,
)
from codey.runs.trace_values import _bounded_refs, _identifier, _nonnegative_int


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
    scope: str = ""
    expires_at: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": _identifier(self.name, 80),
            "digest": self.digest,
            "chars": _nonnegative_int(self.chars),
            "model_visible": self.model_visible,
            "truncated": self.truncated,
        }
        if self.purpose:
            payload["purpose"] = _clip(self.purpose, 160)
        if self.budget:
            payload["budget"] = _nonnegative_int(self.budget)
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
        if self.scope:
            payload["scope"] = _clip(self.scope, 160)
        if self.expires_at:
            payload["expires_at"] = _clip(self.expires_at, 80)
        return payload


@dataclass(frozen=True)
class PromptSurfaceTrace:
    surface_id: str
    phase: str
    prompt_digest: str
    prompt_chars: int
    epoch_id: str
    send_ref: str = ""
    source_refs: tuple[str, ...] = ()
    provider_effect_id: str = ""
    model_tool_contract_hash: str = ""
    runtime_tool_contract_hash: str = ""
    sections: tuple[dict[str, object], ...] = ()
    schema_version: int = 1

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "surface_id": _clip(self.surface_id, 120),
            "phase": _identifier(self.phase, 40),
            "prompt_digest": _clip(self.prompt_digest, 80),
            "prompt_chars": _nonnegative_int(self.prompt_chars),
            "epoch_id": _identifier(self.epoch_id, 80),
        }
        if self.send_ref:
            payload["send_ref"] = _clip(self.send_ref, 80)
        refs = _bounded_refs(self.source_refs)
        if refs:
            payload["source_refs"] = list(refs)
        if self.provider_effect_id:
            payload["provider_effect_id"] = _clip(self.provider_effect_id, 80)
        if self.model_tool_contract_hash:
            payload["model_tool_contract_hash"] = _clip(self.model_tool_contract_hash, 80)
        if self.runtime_tool_contract_hash:
            payload["runtime_tool_contract_hash"] = _clip(self.runtime_tool_contract_hash, 80)
        if self.sections:
            payload["sections"] = [dict(item) for item in self.sections[:8]]
        return payload


@dataclass(frozen=True)
class ModeSelectionTrace:
    """Mode-selection trace written by the live task-dispatch path."""

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
            "overridden_by_user": self.overridden_by_user,
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
    api_usage: list[dict[str, object]] = field(default_factory=list)
    api_usage_latest: dict[str, object] = field(default_factory=dict)
    api_usage_totals: dict[str, int] = field(default_factory=lambda: {
        "requests": 0, "known_input_tokens": 0, "known_output_tokens": 0, "incomplete_requests": 0})
    permission_profiles: list[dict[str, str]] = field(default_factory=list)
    mode_selection: ModeSelectionTrace | None = None
    prompt_sections: list[PromptSectionTrace] = field(default_factory=list)
    prompt_surfaces: list[PromptSurfaceTrace] = field(default_factory=list)
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
    coding_review: dict[str, object] = field(default_factory=dict)
    completion_proofs: list[dict[str, object]] = field(default_factory=list)
    completion_edit_integrity: list[dict[str, object]] = field(default_factory=list)
    research_source_trust: list[dict[str, object]] = field(default_factory=list)
    research_brief_projections: list[dict[str, object]] = field(default_factory=list)
    research_topic_continuity: list[dict[str, object]] = field(default_factory=list)
    completion_repair_context: list[dict[str, object]] = field(default_factory=list)
    protocol_telemetry: dict[str, dict[str, object]] = field(default_factory=dict)
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
            "mode_selection": self.mode_selection.to_payload() if self.mode_selection else {},
            "prompt_sections": [
                item.to_payload() for item in self.prompt_sections[:MAX_PROMPT_SECTIONS]
            ],
            "prompt_surfaces": [
                item.to_payload() for item in self.prompt_surfaces[:MAX_PROMPT_SURFACES]
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
            "coding_review": dict(self.coding_review),
            "completion_proofs": self.completion_proofs[:MAX_COMPLETION_PROOFS],
            "completion_edit_integrity": (
                self.completion_edit_integrity[:MAX_EDIT_INTEGRITY_ROWS]
            ),
            "research_source_trust": self.research_source_trust[:MAX_SOURCE_TRUST_ROWS],
            "research_brief_projections": (
                self.research_brief_projections[:MAX_BRIEF_PROJECTIONS]
            ),
            "research_topic_continuity": (
                self.research_topic_continuity[:MAX_TOPIC_CONTINUITY_ROWS]
            ),
            "completion_repair_context": (
                self.completion_repair_context[:MAX_COMPLETION_REPAIR_ROWS]
            ),
            "protocol_telemetry": _protocol_telemetry_payload(
                self.protocol_telemetry
            ),
            "fallbacks": [item.to_payload() for item in self.fallbacks[:MAX_FALLBACKS]],
            "provider_failures": self.provider_failures[:MAX_FAILURES],
            "policy_decisions": self.policy_decisions[:MAX_POLICY_DECISIONS],
            "warnings": list(_bounded_refs(self.warnings, limit=MAX_WARNINGS)),
            "status": _identifier(self.status, 40),
        }
        if self.api_usage_totals["requests"]:
            payload["api_usage"] = self.api_usage
            payload["api_usage_latest"] = dict(self.api_usage_latest)
            payload["api_usage_totals"] = dict(self.api_usage_totals)
            payload["api_usage_truncated"] = self.api_usage_totals["requests"] > len(self.api_usage)
        return payload
