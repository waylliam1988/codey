"""Deterministic follow-up hints after an approved shell command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from codey.verification_policy import VerificationCandidate


MAX_OUTPUT_SCAN_CHARS = 4_000
MAX_HINTS = 5
MAX_VERIFICATION_HINTS = 2


@dataclass(frozen=True)
class ShellFollowupInput:
    risk_label: str = "generic"
    exit_code: int | None = None
    output: str = ""
    truncated: bool = False
    verification_candidates: tuple[VerificationCandidate, ...] = ()


def render_shell_followup(data: ShellFollowupInput) -> str:
    """Render short internal guidance for the Writer after shell approval."""

    hints = [_exit_hint(data.exit_code)]
    risk_label = (data.risk_label or "generic").strip()
    output = str(data.output or "")[:MAX_OUTPUT_SCAN_CHARS]

    hints.extend(_risk_hints(data, risk_label))
    hints.extend(_output_hints(output, truncated=data.truncated))

    clean = _dedupe(hints)[:MAX_HINTS]
    text = "Follow-up hints:\n" + "\n".join(f"- {item}" for item in clean)
    if risk_label != "generic":
        text += (
            "\nUse these follow-up hints as internal guidance; do not quote "
            "this section title to the user."
        )
    return text


def _exit_hint(exit_code: int | None) -> str:
    if exit_code is None:
        return "The approved command did not return a normal exit code."
    if exit_code == 0:
        return "The approved command exited with code 0."
    return f"The approved command failed with exit code {exit_code}."


def _output_hints(output: str, *, truncated: bool) -> list[str]:
    lowered = output.lower()
    hints: list[str] = []
    if truncated:
        hints.append(
            "The shell output was truncated; inspect narrower output before "
            "assuming omitted content is clean."
        )
    if any(marker in lowered for marker in (
        "command not found",
        "not recognized",
        "executable file not found",
        "enoent",
    )):
        hints.append("The executable may be missing or PATH may need refresh.")
    if any(marker in lowered for marker in (
        "permission denied",
        "access is denied",
        "eacces",
        "operation not permitted",
    )):
        hints.append("This may be a permission issue.")
    if any(marker in lowered for marker in ("timed out", "timeout")):
        hints.append("The command may be long-running or stuck.")
    if any(marker in lowered for marker in (
        "network",
        "econnreset",
        "getaddrinfo",
        "unable to resolve",
        "connection refused",
        "etimedout",
    )):
        hints.append("This may be a network or registry access issue.")
    return hints


def _risk_hints(data: ShellFollowupInput, risk_label: str) -> list[str]:
    if risk_label == "dependency_install":
        return _dependency_hints(data)
    if risk_label == "system_install":
        return _system_install_hints(data)
    if risk_label == "external_source":
        return _external_source_hints(data)
    if risk_label == "dev_server":
        return _dev_server_hints(data)
    if risk_label == "publish":
        return _publish_hints(data)
    return ["Inspect the shell exit code and output before claiming success."]


def _dependency_hints(data: ShellFollowupInput) -> list[str]:
    hints = []
    if data.exit_code == 0:
        hints.append(
            "If dependencies changed manifest or lockfiles, inspect those "
            "changes before done."
        )
        hints.extend(_verification_hints(data.verification_candidates))
    else:
        hints.append("Inspect the install output before proposing another command.")
        hints.append(
            "Do not retry broader install commands without explaining the next "
            "approval."
        )
    hints.append("Do not claim tests passed until a run tool result shows it.")
    return hints


def _system_install_hints(data: ShellFollowupInput) -> list[str]:
    if data.exit_code == 0:
        return [
            "The installed tool may require a new terminal or PATH refresh.",
            "Do not claim the project is verified until a relevant local check has passed.",
        ]
    return [
        "Permission, package-manager availability, or UAC may be involved.",
        "Do not continue installing more system software without a new explanation.",
    ]


def _external_source_hints(data: ShellFollowupInput) -> list[str]:
    if data.exit_code == 0:
        return [
            "Read README or manifest files before running downloaded code.",
            "Do not assume external source is safe or complete without inspection.",
        ]
    return ["Check URL, authentication, network, and destination directory."]


def _dev_server_hints(data: ShellFollowupInput) -> list[str]:
    hints = ["Codey is not managing a background dev server in this flow."]
    if data.exit_code == 0:
        hints.insert(
            0,
            "A returned dev-server command does not prove a server is still running.",
        )
    elif data.exit_code is None or "timeout" in str(data.output or "").lower():
        hints.insert(
            0,
            "This may be a long-running server, not necessarily a build failure.",
        )
    else:
        hints.insert(0, "Inspect the dev-server output before proposing another command.")
    hints.extend(_verification_hints(data.verification_candidates))
    return hints


def _publish_hints(data: ShellFollowupInput) -> list[str]:
    if data.exit_code == 0:
        hints = [
            "Confirm output or status before saying anything was pushed or released.",
        ]
        hints.extend(_verification_hints(data.verification_candidates))
        return hints
    return [
        "Do not retry publish automatically; explain the error and next approval."
    ]


def _verification_hints(
    candidates: Sequence[VerificationCandidate],
) -> list[str]:
    preferred = sorted(
        candidates,
        key=lambda item: not item.previously_passed,
    )
    hints: list[str] = []
    for candidate in preferred[:MAX_VERIFICATION_HINTS]:
        hints.append(
            f"A trusted local check is available: {candidate.command} "
            f"{_cwd_phrase(candidate.cwd)}."
        )
    return hints


def _cwd_phrase(cwd: str) -> str:
    clean = str(cwd or ".").strip().replace("\\", "/") or "."
    return "in the project root" if clean == "." else f"in {clean}/"


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        clean = " ".join(str(item or "").split())
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result
