"""Task-completion receipts built from local facts (0.5.0 schema v1).

A receipt is a bounded read model over one finished run's work,
verification, and edit-integrity facts. It says what changed, how much the
green check can be trusted, and -- when verification may have been
weakened -- one short warning the UI can show. It never carries raw
diffs, raw output, or model text.

Trust is a contract, not a score:

- ``trusted``      -- checks passed and integrity observed nothing high-risk
- ``needs_review`` -- checks passed, but high-confidence integrity findings
                      mean the green may have been earned by weakening
                      verification
- ``limited``      -- the run cannot claim trusted verification: the checks
                      did not pass, or the integrity monitor failed and the
                      green cannot be vouched for
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.completion.edit_integrity import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    STATUS_MONITOR_ERROR,
    STATUS_SUSPICIOUS,
    EditIntegrityObservation,
)
from codey.utils.refs import clip, identifier, nonnegative_int


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

    ``trust`` is the contract-level verdict; ``stance``/``source`` mirror
    the completion decision's provenance when one was supplied, so Details
    can show where a green fact actually came from.
    """

    trust: str
    checks_passed: bool = False
    stance: str = ""
    source: str = ""


@dataclass(frozen=True)
class ReceiptIntegrity:
    """The edit-integrity observation the receipt was built from."""

    status: str = "unobserved"
    severity: str = "none"
    reason_codes: tuple[str, ...] = ()
    authorized_test_edit: bool = False


@dataclass(frozen=True)
class TaskReceipt:
    schema_version: int
    display: ReceiptDisplay
    work: ReceiptWork
    verification: ReceiptVerification
    integrity: ReceiptIntegrity = field(default_factory=ReceiptIntegrity)

    def to_dict(self) -> dict:
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
            "verification": {
                "trust": self.verification.trust,
                "checks_passed": self.verification.checks_passed,
            },
            "integrity": {
                "status": self.integrity.status,
                "severity": self.integrity.severity,
            },
        }
        if self.verification.stance:
            payload["verification"]["stance"] = self.verification.stance
            payload["verification"]["source"] = self.verification.source
        if self.integrity.reason_codes:
            payload["integrity"]["reason_codes"] = list(self.integrity.reason_codes)
        if self.integrity.authorized_test_edit:
            payload["integrity"]["authorized_test_edit"] = True
        return payload


def _file_count_text(count: int) -> str:
    if count <= 0:
        return "No files changed"
    return f"{count} file{'s' if count != 1 else ''} changed"


def build_task_receipt(
    changes: dict | None,
    *,
    decision: object = None,
    integrity: EditIntegrityObservation | None = None,
    checks_passed: bool = False,
) -> TaskReceipt:
    """Build the schema-v1 receipt from collected changes and projections.

    ``decision`` (a CompletionDecision) contributes the verification
    provenance shown in Details; ``integrity`` (an EditIntegrityObservation)
    contributes the trust downgrade for high-confidence findings and for
    monitor errors.
    """

    changes = changes if isinstance(changes, dict) else {}
    changed_count = nonnegative_int(changes.get("changed_count"))
    mode = clip(changes.get("mode"), MAX_MODE_CHARS)
    restore_available = mode == "snapshot" and changed_count > 0

    trust = _verification_trust(checks_passed, integrity)
    summary, detail = _display_text(trust, changed_count, checks_passed, integrity)

    stance = str(getattr(getattr(decision, "provenance", None), "stance", "") or "")
    source = str(getattr(getattr(decision, "provenance", None), "source", "") or "")
    if integrity is not None:
        integrity_section = ReceiptIntegrity(
            status=identifier(integrity.status, 20) or "unobserved",
            severity=identifier(integrity.severity, 20) or "none",
            reason_codes=tuple(
                code
                for code in (
                    identifier(item, 80) for item in integrity.reason_codes
                )
                if code
            )[:8],
            authorized_test_edit=bool(integrity.user_authorized_test_edit),
        )
    else:
        integrity_section = ReceiptIntegrity()

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
            checks_passed=bool(checks_passed),
            stance=stance,
            source=source,
        ),
        integrity=integrity_section,
    )


def _verification_trust(
    checks_passed: bool,
    integrity: EditIntegrityObservation | None,
) -> str:
    if not checks_passed:
        return VERIFICATION_TRUST_LIMITED
    if integrity is None:
        return VERIFICATION_TRUST_TRUSTED
    if integrity.status == STATUS_MONITOR_ERROR:
        # The monitor could not observe: the green can stay a run fact,
        # but the receipt cannot vouch for it.
        return VERIFICATION_TRUST_LIMITED
    if (
        integrity.status == STATUS_SUSPICIOUS
        and integrity.severity in (SEVERITY_HIGH, SEVERITY_CRITICAL)
    ):
        return VERIFICATION_TRUST_NEEDS_REVIEW
    return VERIFICATION_TRUST_TRUSTED


def _display_text(
    trust: str,
    changed_count: int,
    checks_passed: bool,
    integrity: EditIntegrityObservation | None,
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
        detail = "Verification monitoring failed"
    return clip(summary, MAX_SUMMARY_CHARS), clip(detail, MAX_DETAIL_CHARS)


def task_receipt_from_payload(payload: object) -> TaskReceipt | None:
    """Validate a persisted receipt payload; unusable input yields None.

    Fail-closed on purpose: readers (ledger projections, headless
    emitters) either get a well-formed schema-v1 receipt or nothing, never
    a half-valid one.
    """

    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return None
    display = payload.get("display")
    work = payload.get("work")
    verification = payload.get("verification")
    integrity = payload.get("integrity")
    if not all(isinstance(section, dict) for section in (display, work, verification, integrity)):
        return None
    summary = clip(display.get("summary"), MAX_SUMMARY_CHARS)
    trust = identifier(verification.get("trust"), 20)
    if not summary or trust not in RECEIPT_TRUSTS:
        return None
    status = identifier(integrity.get("status"), 20) or "unobserved"
    severity = identifier(integrity.get("severity"), 20) or "none"
    reason_codes = tuple(
        code
        for code in (
            identifier(item, 80) for item in integrity.get("reason_codes", ()) or ()
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
            changed_count=nonnegative_int(work.get("changed_count")),
            mode=clip(work.get("mode"), MAX_MODE_CHARS),
            restore_available=work.get("restore_available") is True,
        ),
        verification=ReceiptVerification(
            trust=trust,
            checks_passed=verification.get("checks_passed") is True,
            stance=identifier(verification.get("stance"), 40),
            source=identifier(verification.get("source"), 40),
        ),
        integrity=ReceiptIntegrity(
            status=status,
            severity=severity,
            reason_codes=reason_codes,
            authorized_test_edit=integrity.get("authorized_test_edit") is True,
        ),
    )


__all__ = [
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
