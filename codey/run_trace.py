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
from codey.prompt_envelope import is_model_boundary_freshness
from codey.research.redaction import looks_sensitive_code
from codey.research.shape import digest_ref as _digest_ref
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
CHECKPOINT_FLUSH_INTERVAL = 8
TRUNCATED_TEXT_SUFFIX = "..."
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
        self._prompt_keys: set[tuple[str, str, str, str, tuple[str, ...], bool]] = set()
        self._local_context_keys: set[tuple[str, str, str]] = set()
        self._research_note_keys: set[str] = set()
        self._research_source_keys: set[str] = set()
        self._research_record_keys: set[str] = set()
        self._research_plan_keys: set[str] = set()
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
        )
        key = (item.name, item.digest, item.purpose, item.freshness, refs, item.model_visible)
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

    def record_context_sources(self, sources: Iterable[Any]) -> None:
        changed = False
        for source in sources:
            text = str(getattr(source, "text", "") or "")
            if not text:
                continue
            refs = (f"context_source:{_identifier(getattr(source, 'key', ''), 80)}",)
            item = PromptSectionTrace(
                name=str(getattr(source, "key", "") or "context_source"),
                digest=digest_text(text),
                chars=len(text),
                purpose=str(getattr(source, "why_included", "") or ""),
                model_visible=True,
                budget=max(0, int(getattr(source, "budget", 0) or 0)),
                truncated=bool(getattr(source, "truncated", False)),
                freshness=str(getattr(source, "freshness", "") or ""),
                source_refs=refs,
            )
            key = (
                item.name,
                item.digest,
                item.purpose,
                item.freshness,
                item.source_refs,
                item.model_visible,
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
        digest = _digest_ref(summary.get("record_digest"))
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
        question_digest = _digest_ref(review.get("question_digest"))
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
        digest = _digest_ref(review.get("record_digest"))
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
        question_digest = _digest_ref(plan.get("question_digest"))
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
            "merged_evidence_count": _nonnegative_int(result.get("merged_evidence_count")),
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
        display_digest = _digest_ref(raw.get("display_digest"))
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
