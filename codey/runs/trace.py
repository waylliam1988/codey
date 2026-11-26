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
from pathlib import Path
from typing import Any

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
from codey.runs.trace_models import (
    FallbackTrace as FallbackTrace,
)
from codey.runs.trace_models import (
    ModeSelectionTrace as ModeSelectionTrace,
)
from codey.runs.trace_models import (
    PromptSectionTrace as PromptSectionTrace,
)
from codey.runs.trace_models import (
    PromptSurfaceTrace as PromptSurfaceTrace,
)
from codey.runs.trace_models import (
    RunTraceManifest as RunTraceManifest,
)
from codey.runs.trace_prompt import PromptTraceRecorder
from codey.runs.trace_protocol import (
    _protocol_kind_code,
    _safe_tool_label,
)
from codey.runs.trace_research import source_ref_for_url as source_ref_for_url
from codey.runs.trace_research_recorder import ResearchTraceRecorder
from codey.runs.trace_schema import (
    CHECKPOINT_FLUSH_INTERVAL,
    MAX_API_USAGE_ROWS,
    MAX_COMPLETION_PROOFS,
    MAX_COMPLETION_REPAIR_ROWS,
    MAX_EDIT_INTEGRITY_ROWS,
    MAX_FAILURES,
    MAX_FALLBACKS,
    MAX_PERMISSION_PROFILES,
    MAX_POLICY_DECISIONS,
    MAX_PROTOCOL_ERROR_KINDS,
    MAX_PROTOCOL_UNKNOWN_TOOLS,
    MAX_PROTOCOL_VALID_TURNS,
    MAX_TOOL_CONTRACTS,
    MAX_TRACE_BYTES,
)
from codey.runs.trace_values import (
    _identifier,
    _int_or_none,
    _nonnegative_int,
)
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key, write_json_atomic
from codey.utils.refs import digest_text


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
        self._completion_proof_keys: set[str] = set()
        self._edit_integrity_keys: set[str] = set()
        self._completion_repair_keys: set[str] = set()
        self._policy_keys: set[tuple[str, str, str, str, str]] = set()
        self._api_usage_keys: set[str] = set()
        self._api_usage_lock = threading.RLock()
        self._prompt = PromptTraceRecorder(manifest, checkpoint=self.checkpoint, flush=self.flush)
        self._research = ResearchTraceRecorder(manifest, checkpoint=self.checkpoint, flush=self.flush)

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

    def record_prompt_section(self, name: str, text: object, *, purpose: str='', model_visible: bool=True, budget: int=0, truncated: bool=False, freshness: str='', source_refs: Iterable[object]=(), epoch_id: str='', admission_reason: str='', capability_id: str='') -> None:
        return self._prompt.record_prompt_section(name, text, purpose=purpose, model_visible=model_visible, budget=budget, truncated=truncated, freshness=freshness, source_refs=source_refs, epoch_id=epoch_id, admission_reason=admission_reason, capability_id=capability_id)

    def record_prompt_surface(self, payload: Mapping[str, object]) -> None:
        return self._prompt.record_prompt_surface(payload)

    def record_provider_prompt_boundary(self, section_args: Mapping[str, object], surface_payload: Mapping[str, object] | None=None) -> None:
        return self._prompt.record_provider_prompt_boundary(section_args, surface_payload)

    def record_context_sources(self, sources: Iterable[Any], *, epoch_id: str='', admission_reason: str='') -> None:
        """Record rendered context sources as bounded prompt-section rows.

            Rows are projected through the shared ContextEpoch admission
            projection, so trace rows and snapshots share one ref/digest
            vocabulary. When epoch_id is supplied (the outbound prompt's
            content-addressed epoch), each row is bound to that provider turn;
            the per-source admission_reason wins over the caller's fallback.
            """
        return self._prompt.record_context_sources(sources, epoch_id=epoch_id, admission_reason=admission_reason)

    def record_local_context_refs(self, refs: Iterable[object]) -> None:
        return self._prompt.record_local_context_refs(refs)

    def record_research_notes(self, ids: Iterable[object]) -> None:
        return self._research.record_research_notes(ids)

    def record_research_sources(self, sources: Iterable[object]) -> None:
        return self._research.record_research_sources(sources)

    def record_research_record_summary(self, summary: Mapping[str, object]) -> None:
        return self._research.record_research_record_summary(summary)

    def record_evidence_ledger_write(self, result: Mapping[str, object]) -> None:
        return self._research.record_evidence_ledger_write(result)

    def record_research_proof_review(self, review: Mapping[str, object]) -> None:
        return self._research.record_research_proof_review(review)

    def record_research_plan(self, plan: Mapping[str, object]) -> None:
        return self._research.record_research_plan(plan)

    def record_research_pipeline_result(self, result: object) -> None:
        return self._research.record_research_pipeline_result(result)

    def record_research_connector_errors(self, errors: object) -> None:
        return self._research.record_research_connector_errors(errors)

    def record_research_done_compilation(self, result: Mapping[str, object]) -> None:
        return self._research.record_research_done_compilation(result)

    def record_analysis_run(self, record: Mapping[str, object]) -> None:
        return self._research.record_analysis_run(record)

    def record_artifact_refs(self, refs: Iterable[object]) -> None:
        return self._research.record_artifact_refs(refs)

    def record_reproducibility_capsule(self, capsule: Mapping[str, object]) -> None:
        return self._research.record_reproducibility_capsule(capsule)

    def record_review_findings(self, findings: Iterable[object]) -> None:
        return self._research.record_review_findings(findings)

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
        return self._research.record_planner_gaps(gaps)

    def record_research_source_trust(self, projections: Iterable[object]) -> None:
        """Record bounded source-trust projections (classes and refs only)."""
        return self._research.record_research_source_trust(projections)

    def record_research_brief_projection(self, projection: Mapping[str, object]) -> None:
        """Record one bounded research brief projection (refs + summaries)."""
        return self._research.record_research_brief_projection(projection)

    def record_research_topic_continuity(self, projection: Mapping[str, object], *, epoch_id: str) -> None:
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
        return self._research.record_research_topic_continuity(projection, epoch_id=epoch_id)

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
