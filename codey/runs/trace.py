"""Bounded audit sidecar for one Codey run.

Run Trace is an index over model-visible inputs and local runtime choices.  It
is deliberately not a transcript and not a second execution ledger: raw prompt
text, chat text, source bodies, webpage bodies, and provider raw errors do not
belong here.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from codey.providers.token_accounting import ApiExchangeUsage
from codey.research.guards import (
    valid_digest_ref,
)
from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_completion import (
    project_completion_proof,
    project_completion_repair_context,
    project_edit_integrity,
)
from codey.runs.trace_protocol import (
    _protocol_kind_code,
    _protocol_telemetry_payload,
    _safe_tool_label,
)
from codey.runs.trace_research import (
    _research_connector_error_payload,
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
)
from codey.runs.trace_schema import (
    CHECKPOINT_FLUSH_INTERVAL,
    MAX_ANALYSIS_RUNS,
    MAX_API_USAGE_ROWS,
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
    MAX_PROTOCOL_ERROR_KINDS,
    MAX_PROTOCOL_UNKNOWN_TOOLS,
    MAX_PROTOCOL_VALID_TURNS,
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
    MAX_TRACE_BYTES,
    MAX_WARNINGS,
    SCHEMA_VERSION,
    TRACE_KIND,
)
from codey.runs.trace_values import (
    _bounded_refs,
    _identifier,
    _int_or_none,
    _nonnegative_int,
    _trace_list_items,
)
from codey.runtime.observe.prompt_envelope import is_model_boundary_freshness
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key, write_json_atomic
from codey.utils.refs import coerce_int, digest_text
from codey.workspace.context_epoch import admission_from_rendered_source


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
            "chars": _nonnegative_int(self.chars),
            "model_visible": bool(self.model_visible),
            "truncated": bool(self.truncated),
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


class RunTraceStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, session_id: str, run_id: str) -> Path:
        return self.session_dir(session_id) / f"{_safe_file_stem(run_id)}.json"

    def session_dir(self, session_id: str) -> Path:
        session_component: str = session_key(session_id)
        return self.state_home / "run_traces" / session_component

    def open(
        self,
        *,
        run_id: str,
        session_id: str,
        project: str | Path | None,
        mode_initial: str,
        provider_initial: str,
    ) -> RunTraceRecorder:
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
        self._edit_integrity_keys: set[str] = set()
        self._source_trust_keys: set[str] = set()
        self._brief_projection_keys: set[tuple[str, str]] = set()
        self._topic_continuity_keys: set[str] = set()
        self._completion_repair_keys: set[str] = set()
        self._prompt_surface_keys: set[str] = set()
        self._policy_keys: set[tuple[str, str, str, str, str]] = set()
        self._api_usage_keys: set[str] = set()
        self._api_usage_lock = threading.RLock()

    def record_api_usage(self, record: ApiExchangeUsage) -> None:
        """Persist normalized facts once per physical request, before answer decoding."""
        with self._api_usage_lock:
            if record.exchange_id in self._api_usage_keys:
                return
            self._api_usage_keys.add(record.exchange_id)
            totals = self.manifest.api_usage_totals
            totals["requests"] += 1
            totals["known_input_tokens"] += record.usage.input_tokens or 0
            totals["known_output_tokens"] += record.usage.output_tokens or 0
            if record.usage.input_tokens is None or record.usage.output_tokens is None or record.outcome == "unknown":
                totals["incomplete_requests"] += 1
            if record.purpose == "conversation":
                self.manifest.api_usage_latest = record.to_payload()
            if len(self.manifest.api_usage) < MAX_API_USAGE_ROWS:
                self.manifest.api_usage.append(record.to_payload())
            self.flush()

    def record_mode_selection(
        self,
        *,
        baseline_mode: str,
        selected_mode: str,
        final_mode: str,
        source: str,
        reason_code: str,
        overridden_by_user: bool = False,
    ) -> None:
        """Record mode selection."""
        self.manifest.mode_selection = ModeSelectionTrace(
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

    def _append_prompt_section(
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
    ) -> bool:
        rendered = str(text or "")
        if not rendered:
            return False
        refs = _bounded_refs(source_refs)
        if not refs and model_visible:
            fallback = _identifier(name, 80) or "prompt_section"
            refs = (f"prompt_section:{fallback}",)
        item = PromptSectionTrace(
            name=name,
            digest=digest_text(rendered),
            chars=len(rendered),
            purpose=str(purpose or ""),
            model_visible=bool(model_visible),
            budget=_nonnegative_int(budget),
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
            return False
        self._prompt_keys.add(key)
        self.manifest.prompt_sections.append(item)
        if len(self.manifest.prompt_sections) > MAX_PROMPT_SECTIONS:
            del self.manifest.prompt_sections[:-MAX_PROMPT_SECTIONS]
            self.manifest.warnings.append("prompt_sections_truncated")
        return True

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
        model_boundary = is_model_boundary_freshness(freshness)
        added = self._append_prompt_section(
            name,
            text,
            purpose=purpose,
            model_visible=model_visible,
            budget=budget,
            truncated=truncated,
            freshness=freshness,
            source_refs=source_refs,
            epoch_id=epoch_id,
            admission_reason=admission_reason,
            capability_id=capability_id,
        )
        if model_boundary:
            self.flush()
        elif added:
            self.checkpoint()

    def _append_prompt_surface(self, payload: object) -> bool:
        try:
            from codey.runtime.observe.prompt_surface import validate_prompt_surface_payload
        except Exception:
            return False
        if not isinstance(payload, Mapping):
            return False
        if not validate_prompt_surface_payload(payload):
            return False
        surface_id = _clip(payload.get("surface_id"), 120)
        send_ref = _clip(payload.get("send_ref"), 80)
        if surface_id in self._prompt_surface_keys:
            return False
        # bounded section payloads already validated
        sections: tuple[dict[str, object], ...] = ()
        raw_sections = payload.get("sections")
        if isinstance(raw_sections, (list, tuple)):
            cleaned: list[dict[str, object]] = []
            for item in raw_sections[:8]:
                if not isinstance(item, Mapping):
                    continue
                cleaned.append({
                    "name": _identifier(item.get("name"), 80),
                    "digest": _clip(item.get("digest"), 80),
                    "chars": _nonnegative_int(item.get("chars")),
                    "source_refs": list(
                        _bounded_refs(_trace_list_items(item.get("source_refs")))
                    ),
                    "model_visible": bool(item.get("model_visible", True)),
                    "freshness": _identifier(item.get("freshness"), 80),
                    "epoch_id": _identifier(item.get("epoch_id"), 80),
                    "capability_id": _identifier(item.get("capability_id"), 80),
                })
            sections = tuple(cleaned)
        schema_version = payload["schema_version"]
        if not isinstance(schema_version, int):
            return False
        item = PromptSurfaceTrace(
            surface_id=surface_id,
            phase=_identifier(payload.get("phase"), 40),
            prompt_digest=_clip(payload.get("prompt_digest"), 80),
            prompt_chars=_nonnegative_int(payload.get("prompt_chars")),
            epoch_id=_identifier(payload.get("epoch_id"), 80),
            send_ref=send_ref,
            source_refs=_bounded_refs(_trace_list_items(payload.get("source_refs"))),
            provider_effect_id=_clip(payload.get("provider_effect_id"), 80),
            model_tool_contract_hash=_clip(payload.get("model_tool_contract_hash"), 80),
            runtime_tool_contract_hash=_clip(payload.get("runtime_tool_contract_hash"), 80),
            sections=sections,
            schema_version=schema_version,
        )
        self._prompt_surface_keys.add(surface_id)
        self.manifest.prompt_surfaces.append(item)
        if len(self.manifest.prompt_surfaces) > MAX_PROMPT_SURFACES:
            del self.manifest.prompt_surfaces[:-MAX_PROMPT_SURFACES]
            self.manifest.warnings.append("prompt_surfaces_truncated")
        return True

    def record_prompt_surface(self, payload: Mapping[str, object]) -> None:
        if self._append_prompt_surface(payload):
            self.flush()

    def record_provider_prompt_boundary(
        self,
        section_args: Mapping[str, object],
        surface_payload: Mapping[str, object] | None = None,
    ) -> None:
        if isinstance(section_args, Mapping):
            self._append_prompt_section(
                name=str(section_args.get("name") or ""),
                text=section_args.get("text"),
                purpose=str(section_args.get("purpose") or ""),
                model_visible=bool(section_args.get("model_visible", True)),
                budget=_nonnegative_int(section_args.get("budget")),
                truncated=bool(section_args.get("truncated", False)),
                freshness=str(section_args.get("freshness") or ""),
                source_refs=_trace_list_items(section_args.get("source_refs")),
                epoch_id=str(section_args.get("epoch_id") or ""),
                admission_reason=str(section_args.get("admission_reason") or ""),
                capability_id=str(section_args.get("capability_id") or ""),
            )
        if surface_payload is not None:
            self._append_prompt_surface(surface_payload)
        self.flush()

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

    def record_local_context_refs(self, refs: Iterable[object]) -> None:
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

    def record_coding_review(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        bounded: dict[str, object] = {
            "verdict": str(payload.get("verdict") or "")[:40],
            "status": str(payload.get("status") or "")[:40],
            "origin": str(payload.get("origin") or "fresh")[:40],
            "finding_count": _nonnegative_count(payload.get("finding_count")),
            "scope_digest": str(payload.get("scope_digest") or "")[:16],
            "prompt_digest": str(payload.get("prompt_digest") or "")[:16],
            "diagnostic_count": _nonnegative_count(payload.get("diagnostic_count")),
        }
        self.manifest.coding_review = bounded
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

    def record_completion_repair_context(
        self,
        projection: Mapping[str, object],
        *,
        epoch_id: str,
    ) -> None:
        """Record one bounded repair-context admission (counts + refs only).

        Mirrors the 0.4.12 continuity admission contract: the digest is the
        dedupe and integrity anchor, and the required ``epoch_id`` binds the
        row to the exact outbound provider-send attempt whose prompt carried
        the ``completion_repair_context`` source. Anything empty or
        malformed fails closed without writing a row or touching the dedupe
        key, so an admitted row cannot exist outside a send-boundary
        binding. The payload vocabulary has no field for raw failure text:
        only counts, classes, reason codes, warnings, and proof refs land.
        """
        payload = project_completion_repair_context(projection, epoch_id=epoch_id)
        if payload is None:
            return
        digest = str(payload["digest"])
        if digest in self._completion_repair_keys:
            return
        self._completion_repair_keys.add(digest)
        self.manifest.completion_repair_context.append(payload)
        if len(self.manifest.completion_repair_context) > MAX_COMPLETION_REPAIR_ROWS:
            del self.manifest.completion_repair_context[:-MAX_COMPLETION_REPAIR_ROWS]
            self.manifest.warnings.append("completion_repair_context_truncated")
        self.checkpoint()

    def record_completion_proof(self, proof: Any) -> None:
        """Record one bounded completion proof (refs and statuses only)."""

        payload = project_completion_proof(proof)
        if payload is None:
            return
        proof_id = str(payload["proof_id"])
        if proof_id in self._completion_proof_keys:
            return
        self._completion_proof_keys.add(proof_id)
        self.manifest.completion_proofs.append(payload)
        if len(self.manifest.completion_proofs) > MAX_COMPLETION_PROOFS:
            del self.manifest.completion_proofs[:-MAX_COMPLETION_PROOFS]
            self.manifest.warnings.append("completion_proofs_truncated")
        self.checkpoint()

    def record_edit_integrity(self, observation: Any) -> None:
        """Record one bounded edit-integrity observation (refs and codes).

        Accepts an EditIntegrityObservation, its payload, or a mapping.
        Malformed input is dropped, and a missing or invalid
        observation_ref means the row is not recordable: the trace never
        carries an integrity row that cannot be named.
        """

        payload = project_edit_integrity(observation)
        if payload is None:
            return
        observation_ref = str(payload["observation_ref"])
        if observation_ref in self._edit_integrity_keys:
            return
        self._edit_integrity_keys.add(observation_ref)
        self.manifest.completion_edit_integrity.append(payload)
        if len(self.manifest.completion_edit_integrity) > MAX_EDIT_INTEGRITY_ROWS:
            del self.manifest.completion_edit_integrity[:-MAX_EDIT_INTEGRITY_ROWS]
            self.manifest.warnings.append("completion_edit_integrity_truncated")
        self.checkpoint()

    # --- Protocol telemetry -------------------------------------------
    # Trace-only counters over the JSON tool protocol: which codec ran,
    # how often replies failed classification, how often a protocol repair
    # prompt went out, and which provider turns produced a parseable plan.
    # No raw prompt, reply, or error text has a field here; an unknown tool
    # name lands only as a digest plus an optional safe short identifier.

    def _protocol_phase(self, phase: object) -> dict[str, object]:
        key = _identifier(phase, 40) or "unknown"
        return self.manifest.protocol_telemetry.setdefault(key, {})

    def record_protocol_codec(
        self,
        codec_name: object,
        *,
        phase: str,
        model_tool_contract_hash: object = "",
        runtime_tool_contract_hash: object = "",
    ) -> None:
        row = self._protocol_phase(phase)
        name = _identifier(codec_name, 40)
        if name:
            row["codec_name"] = name
        model_hash = _clip(model_tool_contract_hash, 80)
        if model_hash:
            row["model_tool_contract_hash"] = model_hash
        runtime_hash = _clip(runtime_tool_contract_hash, 80)
        if runtime_hash:
            row["runtime_tool_contract_hash"] = runtime_hash
        self.checkpoint()

    def record_protocol_error(
        self,
        kind: object,
        *,
        phase: str,
        turn: int = 0,
        tool_name: object = "",
    ) -> None:
        del turn
        kind_code = _protocol_kind_code(kind)
        if not kind_code:
            return
        row = self._protocol_phase(phase)
        counts = row.setdefault("protocol_error_counts", {})
        if isinstance(counts, dict):
            counts[kind_code] = min(
                999,
                (_nonnegative_int(counts.get(kind_code)) or 0) + 1,
            )
            if len(counts) > MAX_PROTOCOL_ERROR_KINDS:
                keep = sorted(counts.items(), key=lambda item: -item[1])
                row["protocol_error_counts"] = dict(keep[:MAX_PROTOCOL_ERROR_KINDS])
                row["protocol_error_counts_truncated"] = True
        raw_tool = _clip(tool_name, 120)
        if raw_tool:
            tools = row.setdefault("unknown_tools", [])
            if not isinstance(tools, list):
                tools = row["unknown_tools"] = list[dict[str, object]]()
            digest = digest_text(raw_tool)
            existing = next(
                (
                    item
                    for item in tools
                    if isinstance(item, dict) and item.get("digest") == digest
                ),
                None,
            )
            if existing is not None:
                existing["count"] = min(
                    999,
                    (_nonnegative_int(existing.get("count")) or 0) + 1,
                )
            elif len(tools) < MAX_PROTOCOL_UNKNOWN_TOOLS:
                entry: dict[str, object] = {"digest": digest, "count": 1}
                label = _safe_tool_label(raw_tool)
                if label:
                    entry["label"] = label
                tools.append(entry)
            else:
                row["unknown_tools_truncated"] = True
        self.checkpoint()

    def record_protocol_repair_prompt(
        self,
        kind: object = "",
        *,
        phase: str,
        turn: int = 0,
    ) -> None:
        del turn
        row = self._protocol_phase(phase)
        code = _protocol_kind_code(kind) or "unspecified"
        counts = row.setdefault("repair_prompt_counts", {})
        if isinstance(counts, dict):
            counts[code] = min(
                999,
                (_nonnegative_int(counts.get(code)) or 0) + 1,
            )
        row["repair_prompt_count"] = min(
            999,
            (_nonnegative_int(row.get("repair_prompt_count")) or 0) + 1,
        )
        self.checkpoint()

    def record_protocol_valid_turn(
        self,
        turn: object,
        *,
        phase: str,
        alias_rewrite_count: int = 0,
        arg_repair_counts: Mapping[str, int] | None = None,
    ) -> None:
        value = _nonnegative_int(turn)
        if not value:
            return
        row = self._protocol_phase(phase)
        if "first_valid_turn" not in row:
            row["first_valid_turn"] = value
        turns = row.setdefault("valid_turns", [])
        if not isinstance(turns, list):
            turns = row["valid_turns"] = list[int]()
        alias_rewrites = min(999, _nonnegative_int(alias_rewrite_count))
        if alias_rewrites:
            row["alias_rewrite_count"] = min(
                999,
                (_nonnegative_int(row.get("alias_rewrite_count")) or 0) + alias_rewrites,
            )
        if isinstance(arg_repair_counts, Mapping):
            counts = row.setdefault("arg_repair_counts", {})
            if isinstance(counts, dict):
                for key, cnt in arg_repair_counts.items():
                    code = _protocol_kind_code(key)
                    if not code:
                        continue
                    counts[code] = min(
                        999,
                        (_nonnegative_int(counts.get(code)) or 0) + min(999, _nonnegative_int(cnt)),
                    )
        if turns and turns[-1] == value:
            self.checkpoint()
            return
        if len(turns) >= MAX_PROTOCOL_VALID_TURNS:
            row["valid_turns_truncated"] = True
            self.checkpoint()
            return
        turns.append(value)
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
        payload: dict[str, str] = {
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
        payload: dict[str, object] = {
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


def _action_ref_or_empty(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("action:") and _is_hex_64(text.removeprefix("action:")):
        return text
    return ""


def _nonnegative_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return max(0, int(value.strip()))
    return 0


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _safe_file_stem(value: object) -> str:
    text = _clip(value, 120)
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in text)
    return safe.strip("._") or "run"
