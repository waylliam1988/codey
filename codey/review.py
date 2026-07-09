"""Small two-model review protocol.

The writer model owns all tool use. The review model only receives a compact
diff and returns structured feedback that can be passed back to the writer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


MAX_REVIEW_DIFF_CHARS = 60_000
MAX_REVIEW_LOG_CHARS = 8_000
MAX_FIELD_CHARS = 2_000
MAX_FINDINGS = 8
REVIEW_REPAIR_PROMPT = (
    "Your previous review did not contain a valid JSON object. "
    "Return only the JSON object now, preserving your previous verdict and "
    "concrete findings. No analysis, no explanation, no markdown. "
    "Every findings[].path must still be copied from the Changed files list; "
    "do not invent filenames. "
    '{"verdict":"approved","summary":"Looks good","findings":[]} or '
    '{"verdict":"changes_requested","summary":"One issue found","findings":'
    '[{"path":"<copy path from Changed files>","issue":"Concrete problem",'
    '"suggested_fix":"Small fix"}]}'
)


@dataclass(frozen=True)
class ReviewFinding:
    path: str
    issue: str
    suggested_fix: str = ""


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    summary: str
    findings: list[ReviewFinding]

    @property
    def approved(self) -> bool:
        return self.verdict == "approved"


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[truncated]"


def _json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def parse_review_response(text: str) -> ReviewResult:
    """Parse a review response while tolerating light prose around JSON."""
    for obj in _json_objects(text):
        findings = _parse_findings(obj.get("findings"))
        verdict = _normalize_verdict(obj.get("verdict") or obj.get("status"), findings)
        summary = _clip(
            obj.get("summary")
            or obj.get("message")
            or ("Looks good" if verdict == "approved" else "Changes requested")
        )
        return ReviewResult(verdict=verdict, summary=summary, findings=findings)
    raise ValueError("review response did not contain a JSON object")


def review_repair_prompt() -> str:
    return REVIEW_REPAIR_PROMPT


def parse_review_with_repair(
    first_reply: str,
    send_repair_prompt: Callable[[str], str],
) -> ReviewResult:
    """Parse a reviewer reply, allowing one JSON-only repair turn."""
    try:
        return parse_review_response(first_reply)
    except ValueError:
        return parse_review_response(send_repair_prompt(REVIEW_REPAIR_PROMPT))


def _parse_findings(value: object) -> list[ReviewFinding]:
    if not isinstance(value, list):
        return []
    findings: list[ReviewFinding] = []
    for item in value[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        issue = _clip(item.get("issue") or item.get("problem") or item.get("message"))
        if not issue:
            continue
        findings.append(
            ReviewFinding(
                path=_clip(item.get("path") or item.get("file"), 400),
                issue=issue,
                suggested_fix=_clip(
                    item.get("suggested_fix")
                    or item.get("fix")
                    or item.get("suggestion")
                ),
            )
        )
    return findings


def _normalize_verdict(value: object, findings: list[ReviewFinding]) -> str:
    raw = _clip(value, 80).lower().replace("-", "_").replace(" ", "_")
    if raw in {"approved", "approve", "ok", "pass", "passed", "looks_good"}:
        return "approved"
    if raw in {
        "changes_requested",
        "request_changes",
        "needs_changes",
        "change_requested",
        "failed",
        "fail",
        "rework",
    }:
        return "changes_requested"
    return "changes_requested" if findings else "approved"


def has_reviewable_changes(changes: dict) -> bool:
    return bool(
        changes
        and changes.get("ok")
        and int(changes.get("changed_count") or 0) > 0
        and str(changes.get("diff") or "").strip()
    )


def render_review_prompt(
    *,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str = "",
) -> str:
    files = changes.get("files") if isinstance(changes.get("files"), list) else []
    file_lines = []
    for file in files[:20]:
        if not isinstance(file, dict):
            continue
        path = _clip(file.get("path"), 400)
        status = _clip(file.get("status") or "M", 20)
        additions = int(file.get("additions") or 0)
        deletions = int(file.get("deletions") or 0)
        file_lines.append(f"- {status} {path} +{additions} -{deletions}")
    changed_files = "\n".join(file_lines) if file_lines else "(not listed)"
    raw_diff = "" if changes.get("diff") is None else str(changes.get("diff"))
    diff_was_truncated = bool(changes.get("truncated")) or len(raw_diff) > MAX_REVIEW_DIFF_CHARS
    diff = _clip(raw_diff, MAX_REVIEW_DIFF_CHARS)
    diff_note = (
        "Diff truncation note: the diff was truncated before review. Review only "
        "the visible diff and explicitly avoid assuming omitted hunks are clean.\n\n"
        if diff_was_truncated
        else ""
    )
    log = _clip(recent_log, MAX_REVIEW_LOG_CHARS) or "(no recent tool log)"

    return (
        "You are a careful code reviewer. Review the writer model's completed "
        "code change. You are read-only: do not ask to edit files directly.\n\n"
        "Only request changes for concrete correctness, test, integration, or "
        "user-visible issues. Do not request broad rewrites or style-only cleanup.\n\n"
        "Every findings[].path must be copied from the Changed files list below. "
        "Do not invent filenames. If the issue is a missing test, missing new file, "
        "or missing documentation, use the most relevant changed file as path and "
        "describe the missing file in issue or suggested_fix.\n\n"
        "Return only JSON. No analysis. No explanation. The first character "
        "must be { and the last character must be }.\n"
        "Return exactly one JSON object and no markdown fences:\n"
        '{"verdict":"approved","summary":"Looks good","findings":[]}\n'
        "or\n"
        '{"verdict":"changes_requested","summary":"One issue found","findings":'
        '[{"path":"<copy path from Changed files>","issue":"Concrete problem",'
        '"suggested_fix":"Small fix"}]}\n\n'
        f"Project: {project}\n\n"
        f"Original user task:\n{_clip(task, 6_000)}\n\n"
        f"Writer summary:\n{_clip(writer_summary, 2_000)}\n\n"
        f"Changed files:\n{changed_files}\n\n"
        f"Recent tool log:\n{log}\n\n"
        f"{diff_note}"
        f"Diff:\n{diff}\n"
    )


def render_writer_followup(task: str, review: ReviewResult) -> str:
    lines = [
        "Continue the task in this same project.",
        "A second model reviewed the current diff and found concrete issues.",
        "Treat the review as advisory: verify it against the files, fix only valid issues, run relevant tests, then call done.",
        "Reviewer paths are only clues. If a referenced path does not exist, do not keep using it; list/search/read the real project files instead.",
        "If a finding is invalid after verification and the relevant tests pass, do not invent a change; explain that briefly in done.",
        "",
        "Original user task:",
        _clip(task, 6_000),
        "",
        "Review summary:",
        review.summary,
        "",
        "Review findings:",
    ]
    if review.findings:
        for index, finding in enumerate(review.findings, start=1):
            lines.append(f"{index}. {finding.path or '(unknown path)'}")
            lines.append(f"   Issue: {finding.issue}")
            if finding.suggested_fix:
                lines.append(f"   Suggested fix: {finding.suggested_fix}")
    else:
        lines.append("1. " + review.summary)
    return "\n".join(lines)
