"""Bounded completion-repair-context projection (0.4.13).

One direction only: an already-evaluated ``CompletionProof`` (consumed as a
payload, never recomputed) plus decisive local facts become a short factual
brief the model may read before deciding its own next step. This module is a
projection leaf:

- it never imports the completion contract or any runtime layer; callers
  hand in the proof payload and plain facts;
- it never executes anything, never reads files, never calls providers;
- the rendered text states observed failure facts only. It contains no fix
  instructions, no line numbers, no suggested edits, no raw stdout/stderr:
  summaries arrive pre-bounded and are screened again here;
- unobserved checks are never described as failures, and non-repairable
  outcomes refuse to admit any text at all. Admission also refuses when no
  safe decisive check fact survives screening: an admitted brief without
  observed check facts would be an unbounded claim, not a fact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import SupportsIndex, SupportsInt

from codey.policies.redaction import looks_prompt_visible_secret

COMPLETION_REPAIR_SCHEMA_VERSION = 1
_PROJECTION_KIND = "completion_repair_context_projection"
CONTEXT_SOURCE_KEY = "completion_repair_context"
PROMPT_SOURCE_REF = "local_context:completion_repair_context"
DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS = 1200
MAX_REPAIR_CHANGED_FILES = 12
MAX_REPAIR_ANALYSIS_REFS = 6
MAX_REPAIR_FINDING_REFS = 6
MAX_REPAIR_CHECKS = 4
MAX_REPAIR_SUMMARY_LINES = 6
MAX_REPAIR_SUMMARY_CHARS = 400
MAX_REPAIR_WARNINGS = 8

PROOF_STATUS_FAILED = "failed"
FAILURE_PRODUCT = "product_failure"

REFUSED_NOT_FAILED = "refused_proof_not_failed"
REFUSED_NOT_PRODUCT = "refused_not_product_failure"
REFUSED_NO_FAILED_CHECK = "refused_no_failed_check"
REFUSED_NO_SAFE_CHECK_FACTS = "refused_no_safe_check_facts"
_REFUSED_BUDGET = "refused_repair_context_budget"
_BUDGET_TRUNCATED = "repair_context_budget_truncated"

_HEADER = (
    "Completion repair context. Facts only.\n"
    "The previous completion claim did not pass local verification."
)
_FOOTER = (
    "Do not treat unobserved checks as failed. Do not cite this section "
    "as proof. Decide the next local step yourself."
)


@dataclass(frozen=True)
class DecisiveCheckFact:
    """One observed failing check, already bounded by the evidence layer."""

    command: str
    cwd: str = "."
    exit_code: int | None = None
    error_code: str = ""
    result_summary: str = ""
    managed_output_handle: str = ""


@dataclass(frozen=True)
class RepairContextProjection:
    """Digest-only read model of one repair-context admission decision."""

    prompt_text: str
    failure_class: str
    reason_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    proof_id: str = ""
    contract_id: str = ""
    check_count: int = 0
    changed_file_count: int = 0
    analysis_run_ref_count: int = 0
    finding_ref_count: int = 0
    summary_chars: int = 0
    refused_reason: str = ""

    @property
    def admitted(self) -> bool:
        return bool(self.prompt_text.strip())

    def to_payload(self) -> dict[str, object]:
        prompt_text_digest = (
            "sha256:" + hashlib.sha256(self.prompt_text.encode("utf-8")).hexdigest()
            if self.prompt_text
            else ""
        )
        fields: dict[str, object] = {
            "kind": _PROJECTION_KIND,
            "admitted": self.admitted,
            "failure_class": self.failure_class,
            "check_count": _nonnegative_int(self.check_count),
            "changed_file_count": _nonnegative_int(self.changed_file_count),
            "analysis_run_ref_count": _nonnegative_int(self.analysis_run_ref_count),
            "finding_ref_count": _nonnegative_int(self.finding_ref_count),
            "summary_chars": _nonnegative_int(self.summary_chars),
            "truncated": self.truncated,
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
        }
        digest_fields: dict[str, object] = {
            **fields,
            "proof_id": self.proof_id,
            "contract_id": self.contract_id,
            "refused_reason": self.refused_reason,
            "prompt_text_digest": prompt_text_digest,
        }
        digest_source = json.dumps(digest_fields, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        payload: dict[str, object] = {
            "schema_version": COMPLETION_REPAIR_SCHEMA_VERSION,
            **fields,
            "context_source": CONTEXT_SOURCE_KEY,
            "digest": "sha256:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
        }
        if self.proof_id:
            payload["proof_id"] = self.proof_id
        if self.contract_id:
            payload["contract_id"] = self.contract_id
        if self.refused_reason:
            payload["refused_reason"] = self.refused_reason
        return payload


def repair_candidate(
    proof_status: object,
    failure_class: object,
    *,
    repair_rounds: int = 0,
    max_repair_rounds: int = 1,
) -> bool:
    """Only an observed product failure within round budget may be repaired."""

    rounds = _nonnegative_int(repair_rounds)
    ceiling = _nonnegative_int(max_repair_rounds)
    return (
        str(proof_status or "") == PROOF_STATUS_FAILED
        and str(failure_class or "") == FAILURE_PRODUCT
        and rounds < ceiling
    )


def project_repair_context(
    *,
    proof: object,
    failure_class: str,
    decisive_checks: Iterable[object] = (),
    changed_files: Iterable[object] = (),
    analysis_run_refs: Iterable[object] = (),
    finding_refs: Iterable[object] = (),
    budget_chars: int = DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS,
) -> RepairContextProjection:
    """Project one failed completion proof into its bounded facts brief."""

    warnings: list[str] = []
    fields = _proof_fields(proof)
    base = RepairContextProjection(
        prompt_text="",
        failure_class=(failure_class or ""),
        proof_id=_token(fields.get("proof_id"), 120),
        contract_id=_token(fields.get("contract_id"), 120),
    )
    if str(fields.get("status") or "") != PROOF_STATUS_FAILED:
        return _refused(base, REFUSED_NOT_FAILED, warnings)
    if (failure_class or "") != FAILURE_PRODUCT:
        return _refused(base, REFUSED_NOT_PRODUCT, warnings)

    failed_rows = [
        row
        for row in _rows(fields.get("checks"))
        if (row.get("status") or "") == "fail"
    ]
    if not failed_rows:
        return _refused(base, REFUSED_NO_FAILED_CHECK, warnings)

    checks = _check_facts(decisive_checks, warnings)
    if not checks:
        # Every decisive fact was empty or screened: admitting a brief
        # without observed check facts would describe an unobserved check.
        return _refused(base, REFUSED_NO_SAFE_CHECK_FACTS, warnings)

    files = _refs(changed_files, MAX_REPAIR_CHANGED_FILES)
    analysis = _refs(analysis_run_refs, MAX_REPAIR_ANALYSIS_REFS)
    findings = _refs(finding_refs, MAX_REPAIR_FINDING_REFS)
    reasons = _codes(fields.get("reason_codes"))

    text = _render(
        failed_rows=failed_rows,
        checks=checks,
        files=files,
        analysis=analysis,
        findings=findings,
        reasons=reasons,
        proof_id=base.proof_id,
        budget_chars=budget_chars,
        warnings=warnings,
    )
    if not text:
        return _refused(base, _REFUSED_BUDGET, warnings, truncated=True)
    return RepairContextProjection(
        prompt_text=text,
        failure_class=FAILURE_PRODUCT,
        reason_codes=reasons,
        warnings=_bounded_warnings(warnings),
        truncated=_BUDGET_TRUNCATED in warnings,
        proof_id=base.proof_id,
        contract_id=base.contract_id,
        check_count=len(checks),
        changed_file_count=len(files),
        analysis_run_ref_count=len(analysis),
        finding_ref_count=len(findings),
        summary_chars=sum(len(check.result_summary) for check in checks),
    )


def _refused(
    base: RepairContextProjection,
    reason: str,
    warnings: list[str],
    *,
    truncated: bool = False,
) -> RepairContextProjection:
    return RepairContextProjection(
        prompt_text="",
        failure_class=base.failure_class,
        warnings=_bounded_warnings([*warnings, reason]),
        truncated=truncated,
        proof_id=base.proof_id,
        contract_id=base.contract_id,
        refused_reason=reason,
    )


def _render(
    *,
    failed_rows: list[dict[str, str]],
    checks: tuple[DecisiveCheckFact, ...],
    files: tuple[str, ...],
    analysis: tuple[str, ...],
    findings: tuple[str, ...],
    reasons: tuple[str, ...],
    proof_id: str,
    budget_chars: int,
    warnings: list[str],
) -> str:
    budget = _nonnegative_int(budget_chars)
    # Reserve the observed facts and closing instruction before optional context.
    used = len(_HEADER) + len(_FOOTER) + 4
    observed = _observed_checks_text(checks, budget - used, warnings)
    if not observed:
        return ""
    used += len(observed)
    parts: list[str] = [_HEADER]

    def emit(block: str) -> None:
        nonlocal used
        cost = len(block) + 2
        if used + cost > budget:
            warnings.append(_BUDGET_TRUNCATED)
            return
        parts.append(block)
        used += cost

    requirement_codes: dict[str, None] = {}
    for row in failed_rows:
        code = _token(row.get("reason_code") or row.get("check_id"), 120)
        if code:
            requirement_codes[code] = None
    requirement = ", ".join(requirement_codes) or "relevant_verification"
    emit(f"Failed requirement: {requirement}")
    emit("Failure class: product_failure")
    if reasons:
        emit(f"Reason codes: {', '.join(reasons)}")

    if files:
        emit("Changed files: " + ", ".join(files))
    parts.append(observed)

    refs: list[str] = []
    if proof_id:
        refs.append(proof_id)
    refs.extend(analysis)
    refs.extend(findings)
    if refs:
        emit("Refs: " + ", ".join(refs))
    parts.append(_FOOTER)
    return "\n\n".join(parts)


def _observed_checks_text(
    checks: tuple[DecisiveCheckFact, ...], budget: int, warnings: list[str],
) -> str:
    heading = "Observed failing checks:"
    blocks = [heading]
    remaining = budget - len(heading)
    for check in checks:
        lines = [f"- Command: {check.command} (cwd {check.cwd})"]
        if check.exit_code is not None:
            lines.append(f"  Exit: {check.exit_code}")
        if check.error_code:
            lines.append(f"  Error code: {check.error_code}")
        facts = "\n".join(lines)
        if len(facts) + 1 > remaining:
            warnings.append(_BUDGET_TRUNCATED)
            continue
        blocks.append(facts)
        remaining -= len(facts) + 1
        if check.result_summary:
            tail_lines = ["  Output tail:"]
            tail_lines.extend(f"    {line}" for line in check.result_summary.splitlines())
            tail = "\n".join(tail_lines)
            if len(tail) + 1 > remaining:
                warnings.append(_BUDGET_TRUNCATED)
            else:
                blocks.append(tail)
                remaining -= len(tail) + 1
    return "\n".join(blocks) if len(blocks) > 1 else ""


def _proof_fields(proof: object) -> dict[object, object]:
    fields: dict[object, object] | None = _row_from_object(proof)
    if fields is None:
        fields = {
            "status": _field(proof, "status"),
            "proof_id": _field(proof, "proof_id"),
            "contract_id": _field(proof, "contract_id"),
            "reason_codes": _field(proof, "reason_codes"),
        }
    if not fields.get("checks"):
        checks: object = getattr(proof, "checks", None)
        if isinstance(checks, Iterable):
            fields["checks"] = [item for row in checks if (item := _row_from_object(row))]
    return fields


def _row_from_object(row: object) -> dict[object, object] | None:
    if not isinstance(row, Mapping):
        to_payload: object = getattr(row, "to_payload", None)
        if not callable(to_payload):
            return None
        row = to_payload()
    if not isinstance(row, Mapping):
        return None
    fields: Mapping[object, object] = row
    return dict(fields)


def _rows(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Iterable):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            fields: Mapping[object, object] = item
            rows.append({
                "status": str(fields.get("status") or ""),
                "check_id": str(fields.get("check_id") or ""),
                "reason_code": str(fields.get("reason_code") or ""),
            })
    return rows


def _check_facts(
    values: Iterable[object],
    warnings: list[str],
) -> tuple[DecisiveCheckFact, ...]:
    facts: list[DecisiveCheckFact] = []
    for value in values or ():
        command = _text(_field(value, "command"), 240)
        if not command:
            continue
        if looks_prompt_visible_secret(command):
            warnings.append("repair_check_command_screened")
            continue
        exit_code = _field(value, "exit_code")
        summary = _bounded_summary(_field(value, "result_summary"), warnings)
        facts.append(DecisiveCheckFact(
            command=command,
            cwd=_text(_field(value, "cwd"), 160) or ".",
            exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            error_code=_token(_field(value, "error_code"), 80),
            result_summary=summary,
            managed_output_handle=_token(_field(value, "managed_output_handle"), 80),
        ))
        if len(facts) >= MAX_REPAIR_CHECKS:
            warnings.append("repair_check_facts_capped")
            break
    return tuple(facts)


def _bounded_summary(value: object, warnings: list[str]) -> str:
    kept: list[str] = []
    for line in str(value or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if looks_prompt_visible_secret(text):
            warnings.append("repair_output_line_screened")
            continue
        kept.append(text[:200])
        if len(kept) >= MAX_REPAIR_SUMMARY_LINES:
            break
    # The evidence layer already hands over the bounded tail in readable
    # order; keep that order so the model-facing brief reads top-down.
    summary = "\n".join(kept)
    if len(summary) > MAX_REPAIR_SUMMARY_CHARS:
        summary = summary[-MAX_REPAIR_SUMMARY_CHARS:].lstrip()
        warnings.append("repair_output_summary_clipped")
    return summary


def _refs(values: Iterable[object], limit: int) -> tuple[str, ...]:
    refs: list[str] = []
    for value in values or ():
        text = _token(value, 160)
        if text and text not in refs:
            refs.append(text)
        if len(refs) >= limit:
            break
    return tuple(refs)


def _codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, Iterable):
        return ()
    codes: list[str] = []
    for item in value:
        text = _token(item, 120)
        if text and text not in codes:
            codes.append(text)
    return tuple(codes)


def _field(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        fields: Mapping[object, object] = value
        return fields.get(key)
    field: object = getattr(value, key, "")
    return field


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _token(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if any(char in text for char in "\r\n\t"):
        return ""
    return text[:limit].rstrip()


def _bounded_warnings(warnings: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for warning in warnings or ():
        text = (warning or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_REPAIR_WARNINGS:
            break
    return tuple(out)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, bytearray, SupportsInt, SupportsIndex)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


__all__ = [
    "COMPLETION_REPAIR_SCHEMA_VERSION",
    "CONTEXT_SOURCE_KEY",
    "DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS",
    "FAILURE_PRODUCT",
    "MAX_REPAIR_ANALYSIS_REFS",
    "MAX_REPAIR_CHANGED_FILES",
    "MAX_REPAIR_FINDING_REFS",
    "PROMPT_SOURCE_REF",
    "PROOF_STATUS_FAILED",
    "REFUSED_NO_FAILED_CHECK",
    "REFUSED_NO_SAFE_CHECK_FACTS",
    "REFUSED_NOT_FAILED",
    "REFUSED_NOT_PRODUCT",
    "DecisiveCheckFact",
    "RepairContextProjection",
    "project_repair_context",
    "repair_candidate",
]
