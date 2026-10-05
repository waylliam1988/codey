"""Task-completion receipts built from local facts (0.5.0 schema v1).

A receipt is a bounded read model over one finished run's work,
verification, and edit-integrity facts. It says what changed, how much the
green check can be trusted, and -- when verification may have been
weakened -- one short warning the UI can show. It never carries raw
diffs, raw output, or model text.

Trust is a contract, not a score:

- ``trusted``      -- checks passed and a present integrity observation
                      found nothing high-risk
- ``needs_review`` -- checks passed, but high-confidence integrity findings
                      mean the green may have been earned by weakening
                      verification
- ``limited``      -- the run cannot claim trusted verification: the checks
                      did not pass, the monitor failed or was incomplete, or
                      no integrity observation was supplied at all

The receipt is the durable audit contract, so it carries the refs it is
derived from (proof ids, the integrity observation ref and affected
paths) instead of forcing readers back into the trace to reconstruct
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, SupportsIndex, SupportsInt, TypeAlias, cast

from codey.completion.edit_integrity import (
    EDIT_INTEGRITY_SEVERITIES,
    EDIT_INTEGRITY_STATUSES,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_NONE,
    STATUS_MONITOR_ERROR,
    STATUS_SUSPICIOUS,
    STATUS_UNOBSERVED,
    EditIntegrityObservation,
)
from codey.policies.redaction import looks_prompt_visible_secret
from codey.utils.refs import clip, identifier

RECEIPT_SCHEMA_VERSION = 1

VERIFICATION_TRUST_TRUSTED = "trusted"
VERIFICATION_TRUST_NEEDS_REVIEW = "needs_review"
VERIFICATION_TRUST_LIMITED = "limited"
RECEIPT_TRUSTS = frozenset(
    {
        VERIFICATION_TRUST_TRUSTED,
        VERIFICATION_TRUST_NEEDS_REVIEW,
        VERIFICATION_TRUST_LIMITED,
    }
)

MAX_SUMMARY_CHARS = 200
MAX_DETAIL_CHARS = 200
MAX_MODE_CHARS = 40
MAX_PROOF_REFS = 2
MAX_INTEGRITY_REFS = 4
MAX_AFFECTED_PATHS = 4

_INT_INPUT: TypeAlias = str | bytes | bytearray | SupportsInt | SupportsIndex


@dataclass(frozen=True)
class ReceiptDisplay:
    """What the UI shows. ``detail`` is for the Details view only."""

    summary: str
    detail: str = ""


@dataclass(frozen=True)
class ReceiptWork:
    """The bounded work facts of the run's final change collection."""

    changed_count: int
    mode: str = ""
    restore_available: bool = False


@dataclass(frozen=True)
class ReceiptVerification:
    """The receipt's stance on the run's verification claim.

    ``trust`` is the contract-level verdict. ``state`` mirrors the
    underlying proof status, ``stance``/``source`` mirror the decision's
    provenance, and ``proof_refs`` name the proof and contract, so
    Details and headless consumers never have to guess from the trace.
    """

    trust: str
    checks_passed: bool = False
    state: str = ""
    stance: str = ""
    source: str = ""
    proof_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceiptIntegrity:
    """The edit-integrity observation the receipt was built from."""

    status: str = "unobserved"
    severity: str = "none"
    reason_codes: tuple[str, ...] = ()
    authorized_test_edit: bool = False
    affected_paths: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskReceipt:
    schema_version: int
    display: ReceiptDisplay
    work: ReceiptWork
    verification: ReceiptVerification
    integrity: ReceiptIntegrity = field(default_factory=ReceiptIntegrity)

    def to_dict(self) -> dict[str, Any]:
        verification_payload: dict[str, object] = {
            "trust": self.verification.trust,
            "checks_passed": self.verification.checks_passed,
        }
        integrity_payload: dict[str, object] = {
            "status": self.integrity.status,
            "severity": self.integrity.severity,
        }
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "display": {
                "summary": self.display.summary,
                "detail": self.display.detail,
            },
            "work": {
                "changed_count": self.work.changed_count,
                "mode": self.work.mode,
                "restore_available": self.work.restore_available,
            },
            "verification": verification_payload,
            "integrity": integrity_payload,
        }
        if self.verification.state:
            verification_payload["state"] = self.verification.state
        if self.verification.stance:
            verification_payload["stance"] = self.verification.stance
            verification_payload["source"] = self.verification.source
        if self.verification.proof_refs:
            verification_payload["proof_refs"] = list(self.verification.proof_refs)
        if self.integrity.reason_codes:
            integrity_payload["reason_codes"] = list(self.integrity.reason_codes)
        if self.integrity.authorized_test_edit:
            integrity_payload["authorized_test_edit"] = True
        if self.integrity.affected_paths:
            integrity_payload["affected_paths"] = list(self.integrity.affected_paths)
        if self.integrity.refs:
            integrity_payload["refs"] = list(self.integrity.refs)
        return payload


def _file_count_text(count: int) -> str:
    if count <= 0:
        return "No files changed"
    return f"{count} file{'s' if count != 1 else ''} changed"


def build_task_receipt(
    changes: dict[str, Any] | None,
    *,
    proof: object = None,
    provenance: object = None,
    integrity: EditIntegrityObservation | None = None,
    checks_passed: bool = False,
) -> TaskReceipt:
    """Build the schema-v1 receipt from collected changes and projections.

    ``proof`` and ``provenance`` contribute the state, Details and refs;
    ``integrity`` (an
    EditIntegrityObservation) contributes the trust downgrade for
    high-confidence findings and for monitor errors. A receipt that
    claims passing checks without a vouching observation is ``limited``
    by contract: nobody can vouch for a green nobody watched. The trust
    and display wording are computed by the same primitive helpers the
    persisted-payload reader uses to re-validate, so a receipt can never
    disagree with its own contract.
    """

    changes = changes if isinstance(changes, dict) else {}
    changed_count = _nonnegative_int(changes.get("changed_count"))
    mode = clip(changes.get("mode"), MAX_MODE_CHARS)
    restore_available = mode == "snapshot" and changed_count > 0

    state = identifier(getattr(proof, "status", ""), 40)
    stance = identifier(getattr(provenance, "stance", ""), 40)
    source = identifier(getattr(provenance, "source", ""), 40)
    proof_refs = tuple(
        ref
        for ref in (
            identifier(getattr(proof, "proof_id", ""), 120),
            identifier(getattr(proof, "contract_id", ""), 120),
        )
        if ref
    )[:MAX_PROOF_REFS]

    if integrity is not None:
        status = identifier(integrity.status, 20) or STATUS_UNOBSERVED
        severity = identifier(integrity.severity, 20) or SEVERITY_NONE
        if status not in EDIT_INTEGRITY_STATUSES:
            status = STATUS_MONITOR_ERROR
            severity = SEVERITY_NONE
        elif severity not in EDIT_INTEGRITY_SEVERITIES:
            severity = SEVERITY_NONE
        integrity_section = ReceiptIntegrity(
            status=status,
            severity=severity,
            reason_codes=tuple(
                code
                for code in (
                    identifier(item, 80) for item in integrity.reason_codes
                )
                if code
            )[:8],
            authorized_test_edit=bool(integrity.user_authorized_test_edit),
            affected_paths=tuple(
                path
                for path in (
                    clip(item, 240)
                    for item in integrity.affected_paths
                )
                if path and not looks_prompt_visible_secret(path)
            )[:MAX_AFFECTED_PATHS],
            refs=tuple(
                ref
                for ref in (
                    identifier(integrity.observation_ref, 80),
                    *(
                        identifier(item, 80)
                        for item in (finding.finding_ref for finding in integrity.findings)
                    ),
                )
                if ref
            )[:MAX_INTEGRITY_REFS],
        )
    else:
        # No observation object: the receipt still states the honest
        # unobserved verdict, and the shared trust helper downgrades it.
        integrity_section = ReceiptIntegrity()

    checks_passed_bool = checks_passed if type(checks_passed) is bool else False
    trust = _verification_trust_from_status(
        checks_passed=checks_passed_bool,
        integrity_status=integrity_section.status,
        integrity_severity=integrity_section.severity,
        changed_count=changed_count,
    )
    summary, detail = _display_text(trust, changed_count, checks_passed_bool)

    return TaskReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        display=ReceiptDisplay(summary=summary, detail=detail),
        work=ReceiptWork(
            changed_count=changed_count,
            mode=mode,
            restore_available=restore_available,
        ),
        verification=ReceiptVerification(
            trust=trust,
            checks_passed=checks_passed_bool,
            state=state,
            stance=stance,
            source=source,
            proof_refs=proof_refs,
        ),
        integrity=integrity_section,
    )


def _verification_trust_from_status(
    *,
    checks_passed: bool,
    integrity_status: str,
    integrity_severity: str,
    changed_count: int,
) -> str:
    """The trust contract over primitive facts only.

    Both the builder and the persisted-payload reader run this exact
    function, so a stored receipt whose trust disagrees with its own
    facts is unrepresentable. A missing observation object collapses
    into ``STATUS_UNOBSERVED`` upstream.
    """

    if not checks_passed:
        return VERIFICATION_TRUST_LIMITED
    if integrity_status == STATUS_MONITOR_ERROR:
        # The monitor could not observe: the green can stay a run fact,
        # but the receipt cannot vouch for it.
        return VERIFICATION_TRUST_LIMITED
    if changed_count > 0 and integrity_status == STATUS_UNOBSERVED:
        # Files changed and checks passed, yet nothing was observed: the
        # same unwatched-green hole, reached through the observation's
        # own verdict instead of a missing argument.
        return VERIFICATION_TRUST_LIMITED
    if (
        integrity_status == STATUS_SUSPICIOUS
        and integrity_severity in (SEVERITY_HIGH, SEVERITY_CRITICAL)
    ):
        return VERIFICATION_TRUST_NEEDS_REVIEW
    return VERIFICATION_TRUST_TRUSTED


def _display_text(
    trust: str,
    changed_count: int,
    checks_passed: bool,
) -> tuple[str, str]:
    parts = [_file_count_text(changed_count)]
    if trust == VERIFICATION_TRUST_TRUSTED and checks_passed:
        parts.append("checks passed")
    elif trust == VERIFICATION_TRUST_NEEDS_REVIEW:
        parts.append("checks need review")
    elif trust == VERIFICATION_TRUST_LIMITED and checks_passed:
        parts.append("verification limited")
    summary = " · ".join(parts)

    detail = ""
    if trust == VERIFICATION_TRUST_NEEDS_REVIEW:
        detail = "Test changes may have weakened verification"
    elif trust == VERIFICATION_TRUST_LIMITED and checks_passed:
        detail = "Verification monitoring incomplete"
    return clip(summary, MAX_SUMMARY_CHARS), clip(detail, MAX_DETAIL_CHARS)


def _receipt_sections_well_formed(display: dict[str, Any], work: dict[str, Any], verification: dict[str, Any], integrity: dict[str, Any]) -> bool:
    """Check field shapes before any normalization or trust recomputation."""
    if not isinstance(display.get("summary"), str) or not isinstance(display.get("detail"), str):
        return False
    if not isinstance(work.get("mode"), str) or not isinstance(work.get("restore_available"), bool):
        return False
    if not isinstance(verification.get("trust"), str) or not isinstance(verification.get("checks_passed"), bool):
        return False
    for key in ("state", "stance", "source"):
        if key in verification and not isinstance(verification.get(key), str):
            return False
    if not isinstance(integrity.get("status"), str) or not isinstance(integrity.get("severity"), str):
        return False
    if not _optional_string_sequence(verification, "proof_refs"):
        return False
    if not _optional_string_sequence(integrity, "reason_codes"):
        return False
    if not _optional_string_sequence(integrity, "affected_paths"):
        return False
    if not _optional_string_sequence(integrity, "refs"):
        return False
    return "authorized_test_edit" not in integrity or isinstance(integrity.get("authorized_test_edit"), bool)


def _receipt_section(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def task_receipt_from_payload(payload: object) -> TaskReceipt | None:
    """Validate a persisted receipt payload; unusable input yields None.

    Fail-closed twice over: the schema shape must be well-formed, and the
    contract must recompute. The reader runs the same trust helper and
    display-wording helper the builder used, so a stored receipt whose
    trust or copy disagrees with its own facts (a tampered or hand-built
    payload) is rejected outright instead of being echoed back as valid.
    """

    if not isinstance(payload, dict):
        return None
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return None
    display = _receipt_section(payload.get("display"))
    work = _receipt_section(payload.get("work"))
    verification = _receipt_section(payload.get("verification"))
    integrity = _receipt_section(payload.get("integrity"))
    if any(section is None for section in (display, work, verification, integrity)):
        return None
    assert display is not None
    assert work is not None
    assert verification is not None
    assert integrity is not None
    if not _receipt_sections_well_formed(display, work, verification, integrity):
        return None
    changed_count = _strict_nonnegative_int(work.get("changed_count"))
    if changed_count is None:
        return None
    summary = clip(display.get("summary"), MAX_SUMMARY_CHARS)
    trust = identifier(verification.get("trust"), 20)
    status = identifier(integrity.get("status"), 20)
    severity = identifier(integrity.get("severity"), 20)
    if not summary or trust not in RECEIPT_TRUSTS:
        return None
    if status not in EDIT_INTEGRITY_STATUSES or severity not in EDIT_INTEGRITY_SEVERITIES:
        return None
    checks_passed = verification.get("checks_passed") is True
    # Contract recomputation: trust and user-facing wording must be
    # exactly what the builder would produce from these facts.
    expected_trust = _verification_trust_from_status(
        checks_passed=checks_passed,
        integrity_status=status,
        integrity_severity=severity,
        changed_count=changed_count,
    )
    if trust != expected_trust:
        return None
    expected_summary, expected_detail = _display_text(trust, changed_count, checks_passed)
    if summary != expected_summary or clip(display.get("detail"), MAX_DETAIL_CHARS) != expected_detail:
        return None
    reason_codes = tuple(
        code
        for code in (
            identifier(item, 80) for item in _string_sequence(integrity, "reason_codes")
        )
        if code
    )[:8]
    return TaskReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        display=ReceiptDisplay(
            summary=summary,
            detail=clip(display.get("detail"), MAX_DETAIL_CHARS),
        ),
        work=ReceiptWork(
            changed_count=changed_count,
            mode=clip(work.get("mode"), MAX_MODE_CHARS),
            restore_available=work.get("restore_available") is True,
        ),
        verification=ReceiptVerification(
            trust=trust,
            checks_passed=checks_passed,
            state=identifier(verification.get("state"), 40),
            stance=identifier(verification.get("stance"), 40),
            source=identifier(verification.get("source"), 40),
            proof_refs=tuple(
                ref
                for ref in (
                    identifier(item, 120)
                    for item in _string_sequence(verification, "proof_refs")
                )
                if ref
            )[:MAX_PROOF_REFS],
        ),
        integrity=ReceiptIntegrity(
            status=status,
            severity=severity,
            reason_codes=reason_codes,
            authorized_test_edit=integrity.get("authorized_test_edit") is True,
            affected_paths=tuple(
                path
                for path in (
                    clip(item, 240)
                    for item in _string_sequence(integrity, "affected_paths")
                )
                if path and not looks_prompt_visible_secret(path)
            )[:MAX_AFFECTED_PATHS],
            refs=tuple(
                ref
                for ref in (
                    identifier(item, 80)
                    for item in _string_sequence(integrity, "refs")
                )
                if ref
            )[:MAX_INTEGRITY_REFS],
        ),
    )


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    strict = _strict_nonnegative_int(value)
    if strict is not None:
        return strict
    if isinstance(value, str) and not value.strip().isascii():
        return 0
    try:
        return max(0, int(cast(_INT_INPUT, value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _strict_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_string_sequence(payload: dict[str, object], key: str) -> bool:
    if key not in payload:
        return True
    value = payload.get(key)
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _string_sequence(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


__all__ = [
    "MAX_AFFECTED_PATHS",
    "MAX_INTEGRITY_REFS",
    "MAX_PROOF_REFS",
    "MAX_SUMMARY_CHARS",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_TRUSTS",
    "ReceiptDisplay",
    "ReceiptIntegrity",
    "ReceiptVerification",
    "ReceiptWork",
    "TaskReceipt",
    "VERIFICATION_TRUST_LIMITED",
    "VERIFICATION_TRUST_NEEDS_REVIEW",
    "VERIFICATION_TRUST_TRUSTED",
    "build_task_receipt",
    "task_receipt_from_payload",
]
