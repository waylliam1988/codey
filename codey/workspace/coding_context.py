from __future__ import annotations

import json
from dataclasses import dataclass

from codey.completion.verification_policy import VerificationCandidate

MAX_CONTEXT_FILES = 8


@dataclass(frozen=True)
class CodingContext:
    read_files: tuple[str, ...] = ()
    edit_eligible_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    selected_verification: VerificationCandidate | None = None
    verification_fresh: bool = False
    verification_forbidden: bool = False


def render_coding_context(context: CodingContext) -> str:
    read_files = _clean_paths(context.read_files)
    eligible_files = _clean_paths(context.edit_eligible_files)
    changed_files = _clean_paths(context.changed_files)
    candidate = context.selected_verification
    if not (read_files or eligible_files or changed_files or candidate is not None):
        return ""

    lines = ["Coding current local context:"]
    if read_files:
        lines.append(f"- Files read this run: {_format_paths(read_files)}")
    if eligible_files:
        lines.append(
            "- Existing files eligible for exact edit: "
            f"{_format_paths(eligible_files)}"
        )
    if changed_files:
        if context.verification_fresh:
            label = "Changed files covered by verification"
        elif context.verification_forbidden:
            label = "Changed files not locally verified by request"
        else:
            label = "Changed files needing verification"
        lines.append(f"- {label}: {_format_paths(changed_files)}")
    if context.verification_forbidden and changed_files:
        lines.append("- Verification status: not run because the task forbids local checks.")
    elif candidate is not None:
        if context.verification_fresh:
            lines.append(
                "- Verification covering current changes: "
                f"{_verification_text(candidate)}"
            )
        else:
            lines.extend((
                "- Suggested verification for current changes:",
                f"  {_run_json(candidate)}",
            ))
        status = (
            "passed after the latest edit"
            if context.verification_fresh
            else "not yet passed after the latest edit"
        )
        lines.append(f"- Verification status: {status}.")
    lines.extend((
        "",
        "This is context, not a fixed tool order. Continue with the next useful coding action.",
        "Reply with exactly one JSON object and no other text.",
    ))
    return "\n".join(lines)


def _clean_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for path in paths:
        text = str(path or "").replace("\\", "/").replace("\r", " ").replace("\n", " ")
        text = " ".join(text.split()).strip().strip("/")
        if text and text not in cleaned:
            cleaned.append(text)
    return tuple(sorted(cleaned))


def _format_paths(paths: tuple[str, ...]) -> str:
    visible = paths[:MAX_CONTEXT_FILES]
    rendered = ", ".join(visible)
    omitted = len(paths) - len(visible)
    if omitted > 0:
        rendered = f"{rendered}, ... (+{omitted} more)"
    return rendered


def _run_json(candidate: VerificationCandidate) -> str:
    command, cwd = _candidate_command_cwd(candidate)
    return json.dumps(
        {"tool": "run", "args": {"command": command, "path": cwd}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _verification_text(candidate: VerificationCandidate) -> str:
    command, cwd = _candidate_command_cwd(candidate)
    return f"{command} (path: {cwd})"


def _candidate_command_cwd(candidate: VerificationCandidate) -> tuple[str, str]:
    command = str(candidate.command or "").replace("\r", " ").replace("\n", " ").strip()
    cwd = (
        str(candidate.cwd or ".")
        .replace("\\", "/")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
        or "."
    )
    return command, cwd
