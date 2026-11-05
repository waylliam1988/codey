"""Small two-model review protocol.

The writer model owns all tool use. The review model only receives a compact
diff and returns structured feedback that can be passed back to the writer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from codey.utils.positive_int import positive_int as _positive_int
from codey.utils.text_budget import clip_tail
from codey.workspace.change_set import ChangeAnchor, ChangeSet

MAX_REVIEW_DIFF_CHARS = 60_000
MAX_REVIEW_LOG_CHARS = 8_000
MAX_FIELD_CHARS = 2_000
MAX_FINDINGS = 8
MAX_REVIEW_REPLY_CHARS = 120_000
MAX_REVIEW_CANDIDATE_OBJECTS = 16
MAX_REVIEW_BRACE_SCANS = 256
MAX_FINDING_CANDIDATES = 64
MAX_DIAGNOSTICS = 8
REVIEW_CONTRACT_VERSION = 1
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
    status: str = "complete"
    origin: str = "fresh"
    diagnostics: tuple[str, ...] = ()
    scope: Any | None = None
    identity: Any | None = None
    source_run_id: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict == "approved" and self.is_complete and not self.findings

    @property
    def is_complete(self) -> bool:
        if self.status != "complete":
            return False
        if self.verdict not in {"approved", "changes_requested"}:
            return False
        scope = self.scope
        if scope is not None:
            complete = getattr(scope, "is_complete", None)
            if isinstance(complete, bool):
                return complete
            if callable(complete):
                try:
                    return bool(complete())
                except Exception:
                    return False
        return True

    @property
    def needs_writer_repair(self) -> bool:
        if self.status not in ("complete", "incomplete"):
            return False
        if self.verdict != "changes_requested":
            return False
        return bool(self.findings)


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    return clip_tail(text, limit)


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


def _json_candidates(text: str) -> tuple[list[tuple[dict[str, Any], str]], tuple[str, ...]]:
    """Scan bounded JSON objects, keeping raw text for duplicate-key checks."""
    decoder = json.JSONDecoder()
    source = text or ""
    candidates: list[tuple[dict[str, Any], str]] = []
    diagnostics: list[str] = []
    brace_scans = 0
    index = 0
    while index < len(source):
        if source[index] != "{":
            index += 1
            continue
        brace_scans += 1
        if brace_scans > MAX_REVIEW_BRACE_SCANS:
            diagnostics.append("candidate_scan_budget_exhausted")
            break
        if len(candidates) >= MAX_REVIEW_CANDIDATE_OBJECTS:
            diagnostics.append("candidate_object_budget_exhausted")
            break
        try:
            value, end = decoder.raw_decode(source[index:])
        except json.JSONDecodeError:
            diagnostics.append("malformed_json_object")
            index += 1
            continue
        if isinstance(value, dict):
            candidates.append((value, source[index : index + end]))
        # Nested findings/metadata are part of this object, never new reviews.
        index += end
    return candidates, tuple(diagnostics[:MAX_DIAGNOSTICS])


def _has_duplicate_keys(raw: str) -> bool:
    try:
        json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        return True
    return False


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key: {key}")
        seen.add(key)
        result[key] = value
    return result


def _is_review_candidate(obj: dict[str, Any]) -> bool:
    return any(key in obj for key in ("verdict", "findings"))


def _strict_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def parse_review_response(
    text: str,
    *,
    changes: dict[str, Any] | ChangeSet | None = None,
) -> ReviewResult:
    """Parse a review response while tolerating light prose around JSON."""
    source = text or ""
    if len(source) > MAX_REVIEW_REPLY_CHARS:
        raise ValueError("review reply oversized")
    change_set = _change_set(changes)
    candidates, scan_diagnostics = _json_candidates(source)
    diagnostics: list[str] = list(scan_diagnostics)
    parsed: list[ReviewResult] = []
    incomplete = bool(scan_diagnostics)
    for obj, raw in candidates:
        if not _is_review_candidate(obj):
            diagnostics.append("skipped_non_review_object")
            continue
        if _has_duplicate_keys(raw):
            diagnostics.append("duplicate_contract_keys")
            incomplete = True
            continue
        result = _parse_candidate(obj, change_set, diagnostics)
        if result is None:
            incomplete = True
            continue
        parsed.append(result)
    diagnostics = diagnostics[:MAX_DIAGNOSTICS]
    if not parsed:
        raise ValueError("review response did not contain a valid review object")
    first = parsed[0]
    for other in parsed[1:]:
        if not _same_review(first, other):
            raise ValueError("conflicting review objects")
    if len(parsed) > 1:
        diagnostics.append("duplicate_review_objects")
    return replace(first, status="incomplete" if incomplete else first.status,
                   diagnostics=tuple(dict.fromkeys([*first.diagnostics, *diagnostics]))[:MAX_DIAGNOSTICS])


def _same_review(first: ReviewResult, second: ReviewResult) -> bool:
    return (
        first.verdict == second.verdict
        and first.summary == second.summary
        and first.findings == second.findings
        and first.status == second.status
    )


def _parse_candidate(
    obj: dict[str, Any],
    change_set: ChangeSet | None,
    diagnostics: list[str],
) -> ReviewResult | None:
    if "findings" in obj and not isinstance(obj.get("findings"), list):
        diagnostics.append("findings_not_list")
        return None
    raw_verdict = obj.get("verdict")
    if raw_verdict is not None and not isinstance(raw_verdict, str):
        diagnostics.append("verdict_not_string")
        return None
    raw_summary = obj.get("summary") if "summary" in obj else obj.get("message")
    if raw_summary is not None and not isinstance(raw_summary, str):
        raw_summary = None
    findings, findings_incomplete = _parse_findings(obj.get("findings"), change_set, diagnostics)
    raw_findings = obj.get("findings")
    had_raw_findings = isinstance(raw_findings, list) and len(raw_findings) > 0
    if had_raw_findings and not findings:
        diagnostics.append("no_actionable_findings")
        summary = _strict_text(raw_summary).strip() or "Review incomplete"
        return ReviewResult(
            verdict="unknown",
            summary=_clip(summary, 2000),
            findings=[],
            status="incomplete",
            origin="fresh",
            diagnostics=tuple(diagnostics[:MAX_DIAGNOSTICS]),
        )
    verdict, status = _normalize_verdict(raw_verdict, findings)
    if verdict == "missing":
        diagnostics.append("missing_verdict_without_findings")
        return None
    if verdict == "unknown":
        summary = _strict_text(raw_summary).strip() or "Review incomplete"
        return ReviewResult(
            verdict="unknown",
            summary=_clip(summary, 2000),
            findings=findings,
            status="incomplete",
            origin="fresh",
            diagnostics=tuple(diagnostics[:MAX_DIAGNOSTICS]),
        )
    if verdict == "approved" and findings:
        verdict = "changes_requested"
    summary = _strict_text(raw_summary).strip() or (
        "Looks good" if verdict == "approved" else "Changes requested"
    )
    final_status = "complete" if not findings_incomplete and (verdict == "approved" or findings) else "incomplete"
    return ReviewResult(
        verdict=verdict,
        summary=_clip(summary),
        findings=findings,
        status=final_status,
        origin="fresh",
        diagnostics=tuple(diagnostics[:MAX_DIAGNOSTICS]),
    )


def _canonical_finding_path(raw_path: str, change_set: ChangeSet | None) -> str:
    from codey.reviews.findings import canonical_path

    return canonical_path(raw_path, change_set)


def review_result_payload(review: ReviewResult | None) -> dict[str, Any]:
    """The shared bounded event projection; findings remain in the artifact."""
    if review is None:
        return {"verdict": "unknown", "status": "unavailable", "origin": "fresh", "finding_count": 0}
    identity = review.identity
    return {
        "verdict": review.verdict,
        "status": review.status,
        "origin": review.origin,
        "finding_count": len(review.findings),
        "attempt_id": identity.attempt_id if identity is not None else "",
        "artifact_sha256": identity.artifact_sha256 if identity is not None else "",
        "source_review_run_id": review.source_run_id,
    }


def parse_review_with_repair(
    first_reply: str,
    send_repair_prompt: Callable[[str], str],
    *,
    changes: dict[str, Any] | ChangeSet | None = None,
) -> ReviewResult:
    """Parse a reviewer reply, allowing one JSON-only repair turn."""
    try:
        first = parse_review_response(first_reply, changes=changes)
    except ValueError:
        return parse_review_response(
            send_repair_prompt(REVIEW_REPAIR_PROMPT),
            changes=changes,
        )
    # Already validated issues are useful observations, even with a partial
    # reply. A format-only turn cannot safely retract them.
    if not first.findings and (first.verdict == "unknown" or first.status == "incomplete"):
        return parse_review_response(
            send_repair_prompt(REVIEW_REPAIR_PROMPT),
            changes=changes,
        )
    return first


def _parse_findings(
    value: object,
    change_set: ChangeSet | None = None,
    diagnostics: list[str] | None = None,
) -> tuple[list[ReviewFinding], bool]:
    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], False
    incomplete = len(value) > MAX_FINDING_CANDIDATES
    if incomplete and diagnostics is not None:
        diagnostics.append("finding_candidate_budget_exhausted")
    findings: list[ReviewFinding] = []
    seen: set[tuple[object, ...]] = set()
    for item in value[:MAX_FINDING_CANDIDATES]:
        before = len(diagnostics) if diagnostics is not None else 0
        finding = _parse_one_finding(item, change_set, diagnostics, seen)
        if finding is None:
            if diagnostics is not None:
                incomplete |= any(reason != "duplicate_finding" for reason in diagnostics[before:])
            continue
        findings.append(finding)
    incomplete |= len(findings) > MAX_FINDINGS
    return findings[:MAX_FINDINGS], incomplete


def _parse_one_finding(
    item: object,
    change_set: ChangeSet | None,
    diagnostics: list[str] | None,
    seen: set[tuple[object, ...]],
) -> ReviewFinding | None:
    if not isinstance(item, dict):
        _diag(diagnostics, "invalid_finding_entry")
        return None
    issue = _finding_issue(item, diagnostics)
    if issue is None:
        return None
    canonical = _finding_canonical_path(item, change_set, diagnostics)
    if not canonical:
        return None
    suggested_fix = _finding_fix(item, diagnostics)
    if suggested_fix is None:
        return None
    anchor = _normalized_anchor(
        change_set,
        canonical,
        item.get("hunk_index") if "hunk_index" in item else (item.get("hunk") if "hunk" in item else item.get("hunk_number")),
        item.get("new_line") if "new_line" in item else item.get("line"),
        item.get("old_line") if "old_line" in item else None,
    )
    fingerprint = (canonical, issue, suggested_fix, anchor.hunk_index, anchor.new_line, anchor.old_line)
    if fingerprint in seen:
        _diag(diagnostics, "duplicate_finding")
        return None
    seen.add(fingerprint)
    return ReviewFinding(
        path=canonical,
        issue=_clip(issue),
        suggested_fix=_clip(suggested_fix),
        hunk_index=anchor.hunk_index,
        new_line=anchor.new_line,
        old_line=anchor.old_line,
    )


def _diag(diagnostics: list[str] | None, reason: str) -> None:
    if diagnostics is not None:
        diagnostics.append(reason)


def _finding_issue(item: dict[str, Any], diagnostics: list[str] | None) -> str | None:
    issue_raw = item.get("issue") if "issue" in item else None
    if issue_raw is None:
        issue_raw = item.get("problem") if "problem" in item else item.get("message")
    if not isinstance(issue_raw, str) or not issue_raw.strip():
        _diag(diagnostics, "invalid_finding_issue")
        return None
    return issue_raw.strip()


def _finding_canonical_path(
    item: dict[str, Any],
    change_set: ChangeSet | None,
    diagnostics: list[str] | None,
) -> str:
    path_raw = item.get("path") if "path" in item else item.get("file")
    if path_raw is None:
        _diag(diagnostics, "missing_finding_path")
        return ""
    if not isinstance(path_raw, str) or not path_raw.strip():
        _diag(diagnostics, "invalid_finding_path")
        return ""
    canonical = _canonical_finding_path(path_raw.strip(), change_set)
    if not canonical:
        _diag(diagnostics, "unknown_finding_path")
        return ""
    return canonical


def _finding_fix(item: dict[str, Any], diagnostics: list[str] | None) -> str | None:
    fix_raw = (
        item.get("suggested_fix")
        if "suggested_fix" in item
        else (item.get("fix") if "fix" in item else item.get("suggestion"))
    )
    if fix_raw is None:
        return ""
    if isinstance(fix_raw, str):
        return fix_raw.strip()
    _diag(diagnostics, "invalid_finding_fix")
    return None


def _normalize_verdict(value: object, findings: list[ReviewFinding]) -> tuple[str, str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if findings:
            return "changes_requested", "complete"
        return "missing", "incomplete"
    if not isinstance(value, str):
        return "missing", "incomplete"
    raw = value.strip()[:80].lower().replace("-", "_").replace(" ", "_")
    if raw in {"approved", "approve", "ok", "pass", "passed", "looks_good"}:
        return "approved", "complete"
    if raw in {
        "changes_requested",
        "request_changes",
        "needs_changes",
        "change_requested",
        "failed",
        "fail",
        "rework",
    }:
        return "changes_requested", "complete"
    return "unknown", "incomplete"


def has_reviewable_changes(changes: dict[str, Any]) -> bool:
    return ChangeSet.from_changes(changes).has_reviewable_diff()


def render_review_prompt(
    *,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict[str, Any],
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


def _change_set(changes: dict[str, Any] | ChangeSet | None) -> ChangeSet | None:
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


def _finding_location(finding: ReviewFinding) -> str:
    parts = [finding.path or "(unknown path)"]
    if finding.hunk_index is not None:
        parts.append(f"hunk {finding.hunk_index}")
    if finding.new_line is not None:
        parts.append(f"new line {finding.new_line}")
    if finding.old_line is not None:
        parts.append(f"old line {finding.old_line}")
    return " ".join(parts)
