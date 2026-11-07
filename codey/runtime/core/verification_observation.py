"""Ordered verification facts, including incomplete observations that block reuse.

A malformed latest result is still an observation. Decoding must never drop it
and thereby resurrect an older success for the same command and working dir.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypeAlias, TypedDict

from codey.types.json_value import decode_json_object
from codey.utils.refs import strict_verification_success
from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision


class VerificationRecord(TypedDict):
    """Session wire projection; absence is meaningful for optional fields."""

    command: str
    cwd: str
    revision: int
    passed: bool
    exit_code: NotRequired[int]
    workspace_revision: NotRequired[int]
    workspace_fingerprint: NotRequired[str]


VerificationGap: TypeAlias = Literal[
    "verification_result_missing",
    "verification_identity_missing",
    "verification_identity_invalid",
]


@dataclass(frozen=True)
class CompleteVerification:
    exit_code: int
    workspace_revision: int
    workspace_fingerprint: str
    succeeded: bool


@dataclass(frozen=True)
class IncompleteVerification:
    gap: VerificationGap


@dataclass(frozen=True)
class VerificationObservation:
    command: str
    cwd: str
    edit_revision: int | None
    outcome: CompleteVerification | IncompleteVerification

    @property
    def key(self) -> tuple[str, str]:
        return self.command, self.cwd


def decode_verification_observation(value: object) -> VerificationObservation | None:
    """Decode legacy rows without losing invalid latest identity/result facts.

    Non-record entries historically have no command/revision and are ignored.
    Integer revisions remain separate from workspace revision; a malformed edit
    revision cannot be assigned to a real edit epoch.
    """
    if not isinstance(value, dict):
        return None
    row = decode_json_object(value)
    command = str(row.get("command") or "").strip()
    cwd = str(row.get("cwd") or ".").strip() or "."
    raw_revision = row.get("revision", -1)
    revision = raw_revision if isinstance(raw_revision, int) and not isinstance(raw_revision, bool) else None
    code = row.get("exit_code")
    raw_ws_revision = row.get("workspace_revision")
    raw_fp = row.get("workspace_fingerprint")
    fingerprint = str(raw_fp or "")
    gap: VerificationGap | None = None
    if not command or not isinstance(code, int) or isinstance(code, bool):
        gap = "verification_result_missing"
    elif raw_ws_revision is None or not fingerprint:
        gap = "verification_identity_missing"
    elif (not isinstance(raw_ws_revision, int) or isinstance(raw_ws_revision, bool)
          or not valid_workspace_revision(raw_ws_revision)
          or not valid_workspace_fingerprint(fingerprint)):
        gap = "verification_identity_invalid"
    if gap is not None:
        return VerificationObservation(command, cwd, revision, IncompleteVerification(gap))
    # Repeat the narrowing explicitly; the gap classifier above validates the
    # business rules while these guards establish the concrete constructor types.
    if not isinstance(code, int) or not isinstance(raw_ws_revision, int):
        raise ValueError("complete verification has no integer result or identity")
    return VerificationObservation(command, cwd, revision, CompleteVerification(
        code, raw_ws_revision, fingerprint,
        strict_verification_success(row.get("passed"), code, passed_present="passed" in row),
    ))


def verification_observations(values: Sequence[object]) -> tuple[VerificationObservation, ...]:
    return tuple(row for value in values
                 if (row := value if isinstance(value, VerificationObservation)
                     else decode_verification_observation(value)) is not None)


def latest_verification_observations(
    observations: Sequence[VerificationObservation], edit_revision: int,
) -> dict[tuple[str, str], VerificationObservation]:
    """Input order is authoritative, including incomplete rows for a key."""
    return {row.key: row for row in observations if row.edit_revision == edit_revision}
