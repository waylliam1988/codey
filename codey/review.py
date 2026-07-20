"""Small two-model review protocol.

The writer model owns all tool use. The review model only receives a compact
diff and returns structured feedback that can be passed back to the writer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from codey.change_set import ChangeAnchor, ChangeSet


MAX_REVIEW_DIFF_CHARS = 60_000
MAX_REVIEW_LOG_CHARS = 8_000
MAX_FIELD_CHARS = 2_000
MAX_FINDINGS = 8
REVIEW_REPAIR_PROMPT = (
    "Your previous review did not contain a valid JSON object. "
    "Return only the JSON object now, preserving your previous verdict and "
    "concrete findings. No analysis, no explanation, no markdown. "
    "Every findings[].path must still be copied from the Changed files list; "
    "do not invent filenames or anchors. "
    '{"verdict":"approved","summary":"Looks good","findings":[]} or '
    '{"verdict":"changes_requested","summary":"One issue found","findings":'
    '[{"path":"<copy path from Changed files>","issue":"Concrete problem",'
    '"suggested_fix":"Small fix","hunk_index":1,"new_line":41}]}'
)


@dataclass(frozen=True)
class ReviewFinding:
    path: str
    issue: str
    suggested_fix: str = ""
    hunk_index: int | None = None
    new_line: int | None = None
    old_line: int | None = None


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


def _change_brief_section(change_brief: str) -> str:
    brief = _clip(change_brief, 8_000)
    if not brief:
        return ""
    if brief.lower().startswith("private changebrief"):
        return brief
    return f"Private ChangeBrief:\n{brief}"


def _project_map_section(project_map: str) -> str:
    text = _clip(project_map, 5_000)
    if not text:
        return ""
    return text


def _verification_map_section(verification_map: str) -> str:
    return _clip(verification_map, 5_000)


def _review_impact_map_section(review_impact_map: str) -> str:
    return _clip(review_impact_map, 3_000)


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


def parse_review_response(
    text: str,
    *,
    changes: dict | ChangeSet | None = None,
) -> ReviewResult:
    """Parse a review response while tolerating light prose around JSON."""
    change_set = _change_set(changes)
    for obj in _json_objects(text):
        findings = _parse_findings(obj.get("findings"), change_set)
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
    *,
    changes: dict | ChangeSet | None = None,
) -> ReviewResult:
    """Parse a reviewer reply, allowing one JSON-only repair turn."""
    try:
        return parse_review_response(first_reply, changes=changes)
    except ValueError:
        return parse_review_response(
            send_repair_prompt(REVIEW_REPAIR_PROMPT),
            changes=changes,
        )


def _parse_findings(
    value: object,
    change_set: ChangeSet | None = None,
) -> list[ReviewFinding]:
    if not isinstance(value, list):
        return []
    findings: list[ReviewFinding] = []
    for item in value[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        issue = _clip(item.get("issue") or item.get("problem") or item.get("message"))
        if not issue:
            continue
        path = _clip(item.get("path") or item.get("file"), 400)
        anchor = _normalized_anchor(
            change_set,
            path,
            item.get("hunk_index") or item.get("hunk") or item.get("hunk_number"),
            item.get("new_line") or item.get("line"),
            item.get("old_line"),
        )
        findings.append(
            ReviewFinding(
                path=path,
                issue=issue,
                suggested_fix=_clip(
                    item.get("suggested_fix")
                    or item.get("fix")
                    or item.get("suggestion")
                ),
                hunk_index=anchor.hunk_index,
                new_line=anchor.new_line,
                old_line=anchor.old_line,
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
    return ChangeSet.from_changes(changes).has_reviewable_diff()


def render_review_prompt(
    *,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str = "",
    change_brief: str = "",
    project_map: str = "",
    verification_map: str = "",
    review_impact_map: str = "",
    execution_evidence: str = "",
) -> str:
    change_set = ChangeSet.from_changes(changes)
    files = change_set.files
    file_lines = []
    for file in files[:20]:
        path = _clip(file.path, 400)
        status = _clip(file.status or "M", 20)
        additions = file.additions
        deletions = file.deletions
        file_lines.append(f"- {status} {path} +{additions} -{deletions}")
    changed_files = "\n".join(file_lines) if file_lines else "(not listed)"
    change_summary = _clip(change_set.render_summary(), 8_000)
    raw_diff = change_set.raw_diff
    diff_was_truncated = bool(changes.get("truncated")) or len(raw_diff) > MAX_REVIEW_DIFF_CHARS
    diff = _clip(raw_diff, MAX_REVIEW_DIFF_CHARS)
    diff_note = (
        "Diff truncation note: the diff was truncated before review. Review only "
        "the visible diff and explicitly avoid assuming omitted hunks are clean.\n\n"
        if diff_was_truncated
        else ""
    )
    log = _clip(recent_log, MAX_REVIEW_LOG_CHARS) or "(no recent tool log)"
    brief_section = _change_brief_section(change_brief)
    brief_block = f"{brief_section}\n\n" if brief_section else ""
    map_section = _project_map_section(project_map)
    map_block = f"{map_section}\n\n" if map_section else ""
    verification_section = _verification_map_section(verification_map)
    verification_block = (
        f"{verification_section}\n\n" if verification_section else ""
    )
    impact_section = _review_impact_map_section(review_impact_map)
    impact_block = f"{impact_section}\n\n" if impact_section else ""
    impact_guidance = (
        "The impact map contains bounded local reference hints, not "
        "proof of impact or coverage. Use it to inspect possible affected "
        "callers and tests, but request changes only for concrete issues in "
        "the actual diff. Impact-map paths do not relax the "
        "Changed-files-only findings rule.\n\n"
        if impact_section
        else ""
    )
    evidence = _clip(execution_evidence, 5_000)
    evidence_block = f"{evidence}\n\n" if evidence else ""
    intent_guidance = (
        "Review the change against the Original user task and the private task brief below: "
        "check whether the user intent is satisfied, acceptance checks are covered, "
        "non-goals were not violated, and listed risks were addressed or explicitly "
        "deferred. If one of those fails in a user-visible way, return a concrete "
        "finding tied to the most relevant changed file.\n\n"
        if brief_section
        else
        "Review whether the change satisfies the Original user task. If the task "
        "is incomplete in a user-visible way, return a concrete finding tied to "
        "the most relevant changed file.\n\n"
    )

    return (
        "You are a careful code reviewer. Review the writer model's completed "
        "code change. You are read-only: do not ask to edit files directly.\n\n"
        "Only request changes for concrete correctness, test, integration, or "
        "user-visible issues. Do not request broad rewrites or style-only cleanup.\n\n"
        f"{intent_guidance}"
        "Every findings[].path must be copied from the Changed files list below. "
        "Do not invent filenames. The Project Map is only context for coverage "
        "and integration judgment; never use a path from the Project Map as "
        "findings[].path unless it also appears in Changed files. If the issue "
        "is a missing test, missing new file, or missing documentation, use the "
        "most relevant changed file as path and describe the missing file in "
        "issue or suggested_fix.\n\n"
        "The Verification Map contains bounded candidates, not proof of impact "
        "or coverage. Do not request a test merely because it appears as a "
        "candidate. Request changes only when a candidate is materially relevant "
        "to the diff and the observed checks leave a concrete regression risk "
        "unverified. A listed test candidate was observed as an existing readable "
        "local file; absence from Changed files means it was not modified, not "
        "that it is missing. Paths from the Verification Map do not relax the "
        "Changed-files-only findings rule.\n\n"
        f"{impact_guidance}"
        "If a finding is tied to a specific changed hunk or line, include optional "
        "findings[].hunk_index, findings[].new_line, or findings[].old_line. "
        "Do not invent anchors. Omit them when unsure; path-only findings are valid.\n\n"
        "Return only JSON. No analysis. No explanation. The first character "
        "must be { and the last character must be }.\n"
        "Return exactly one JSON object and no markdown fences:\n"
        '{"verdict":"approved","summary":"Looks good","findings":[]}\n'
        "or\n"
        '{"verdict":"changes_requested","summary":"One issue found","findings":'
        '[{"path":"<copy path from Changed files>","issue":"Concrete problem",'
        '"suggested_fix":"Small fix","hunk_index":1,"new_line":41}]}\n\n'
        f"Project: {project}\n\n"
        f"Original user task:\n{_clip(task, 6_000)}\n\n"
        f"{brief_block}"
        f"{map_block}"
        f"Writer summary:\n{_clip(writer_summary, 2_000)}\n\n"
        f"Changed files:\n{changed_files}\n\n"
        f"{change_summary}\n\n"
        f"{impact_block}"
        f"{evidence_block}"
        f"{verification_block}"
        f"Recent tool log:\n{log}\n\n"
        f"{diff_note}"
        f"Diff:\n{diff}\n"
    )


def render_writer_followup(
    task: str,
    review: ReviewResult,
    *,
    change_brief: str = "",
) -> str:
    lines = [
        "Continue the task in this same project.",
        "A review pass inspected the current diff and found concrete issues.",
        "Treat the review as advisory: verify it against the files, fix only valid issues, run relevant tests, then call done.",
        "Reviewer paths are only clues; anchors are only clues too. If a referenced path does not exist, do not keep using it; list/search/read the real project files instead.",
        "If a finding is invalid after verification and the relevant tests pass, do not invent a change; explain that briefly in done.",
        "",
        "Original user task:",
        _clip(task, 6_000),
    ]
    brief_section = _change_brief_section(change_brief)
    if brief_section:
        lines.extend([
            "",
            brief_section,
        ])
    lines.extend([
        "",
        "Review summary:",
        review.summary,
        "",
        "Review findings:",
    ])
    if review.findings:
        for index, finding in enumerate(review.findings, start=1):
            lines.append(f"{index}. {_finding_location(finding)}")
            lines.append(f"   Issue: {finding.issue}")
            if finding.suggested_fix:
                lines.append(f"   Suggested fix: {finding.suggested_fix}")
    else:
        lines.append("1. " + review.summary)
    return "\n".join(lines)


def _change_set(changes: dict | ChangeSet | None) -> ChangeSet | None:
    if isinstance(changes, ChangeSet):
        return changes
    if isinstance(changes, dict):
        return ChangeSet.from_changes(changes)
    return None


def _normalized_anchor(
    change_set: ChangeSet | None,
    path: str,
    hunk_index: object,
    new_line: object,
    old_line: object,
) -> ChangeAnchor:
    if change_set is not None:
        return change_set.normalize_anchor(path, hunk_index, new_line, old_line)
    return ChangeAnchor(
        _positive_int(hunk_index),
        _positive_int(new_line),
        _positive_int(old_line),
    )


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _finding_location(finding: ReviewFinding) -> str:
    parts = [finding.path or "(unknown path)"]
    if finding.hunk_index is not None:
        parts.append(f"hunk {finding.hunk_index}")
    if finding.new_line is not None:
        parts.append(f"new line {finding.new_line}")
    if finding.old_line is not None:
        parts.append(f"old line {finding.old_line}")
    return " ".join(parts)
