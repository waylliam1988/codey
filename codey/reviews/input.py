"""Safe review input preparation with explicit scope."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from codey.reviews.core import (
    MAX_REVIEW_DIFF_CHARS,
    MAX_REVIEW_LOG_CHARS,
    render_review_prompt,
)
from codey.workspace.change_set import ChangeSet


@dataclass(frozen=True)
class ReviewScope:
    total_changed_files: int = 0
    provided_files: tuple[str, ...] = ()
    excluded_files: tuple[str, ...] = ()
    diff_truncated: bool = False
    file_list_truncated: bool = False
    context_truncated: bool = False
    collection_incomplete: bool = False
    required_context_truncated: bool = False
    content_redacted: bool = False
    exclusion_reasons: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not (
            self.collection_incomplete
            or self.diff_truncated
            or self.file_list_truncated
            or bool(self.excluded_files)
            or self.required_context_truncated
            or self.content_redacted
        )


@dataclass(frozen=True)
class ReviewInput:
    change_set: ChangeSet
    reviewer_view: dict[str, Any]
    prompt: str
    scope: ReviewScope


_SENSITIVE_NAME_PARTS = frozenset({
    "secret", "secrets", "credential", "credentials", "private",
    "apikey", "api_key", "auth",
})
_SENSITIVE_FILENAMES = frozenset({
    ".env", ".env.local", ".env.development", ".env.production",
    ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json", "service-account.json",
})
_SENSITIVE_SUFFIXES = frozenset({".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der"})
_REDACTED = "[REDACTED]"


def prepare_review_input(
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
) -> ReviewInput:
    source = changes if isinstance(changes, dict) else {}
    change_set = ChangeSet.from_changes(source)
    if not isinstance(source, dict) or source.get("ok") is not True:
        scope = ReviewScope(collection_incomplete=True)
        safe_changes = {
            "ok": False,
            "files": [],
            "diff": "",
            "truncated": False,
            "changed_count": 0,
        }
        prompt = render_review_prompt(
            project=project,
            task=_redact_text(task),
            writer_summary=_redact_text(writer_summary),
            changes=safe_changes,
            recent_log=_redact_text(recent_log),
            change_brief=_redact_text(change_brief),
            project_map=_redact_text(project_map),
            verification_map=_redact_text(verification_map),
            review_impact_map=_redact_text(review_impact_map),
            execution_evidence=_redact_text(execution_evidence),
        )
        return ReviewInput(
            change_set=change_set,
            reviewer_view=safe_changes,
            prompt=prompt,
            scope=scope,
        )
    all_files = list(change_set.files)
    total = len(all_files)
    excluded: list[str] = []
    reasons: list[str] = []
    kept: list[str] = []
    allowed_diff_paths: set[str] = set()
    for item in all_files:
        reason = _sensitive_file_reason(item.path)
        if not reason and item.previous_path:
            reason = _sensitive_file_reason(item.previous_path)
        if reason:
            excluded.append(item.path)
            reasons.append(f"{item.path}:{reason}")
        else:
            kept.append(item.path)
    file_list_truncated = len(kept) > 20
    provided = tuple(kept[:20])
    for item in all_files:
        if item.path in provided:
            allowed_diff_paths.add(item.path)
            if item.previous_path:
                allowed_diff_paths.add(item.previous_path)
    raw_diff = str(source.get("diff") or "")
    upstream_truncated = bool(source.get("truncated"))
    filtered_diff, filter_incomplete = _filter_diff_to_provided(raw_diff, allowed_diff_paths)
    redacted_diff = _redact_text(filtered_diff)
    diff_truncated = (
        upstream_truncated
        or filter_incomplete
        or len(redacted_diff) > MAX_REVIEW_DIFF_CHARS
    )
    safe_log = _redact_text(recent_log)
    safe_brief = _redact_text(change_brief)
    safe_map = _redact_text(project_map)
    safe_verification = _redact_text(verification_map)
    safe_impact = _redact_text(review_impact_map)
    safe_evidence = _redact_text(execution_evidence)
    safe_task = _redact_text(task)
    safe_summary = _redact_text(writer_summary)
    context_truncated = any(
        len(text) > limit
        for text, limit in (
            (safe_map, 5000),
            (safe_verification, 5000),
            (safe_impact, 3000),
            (safe_log, MAX_REVIEW_LOG_CHARS),
            (safe_evidence, 5000),
        )
    )
    safe_files = []
    for path in provided:
        original = next((f for f in all_files if f.path == path), None)
        if original is None:
            continue
        safe_files.append({
            "path": original.path,
            "status": original.status,
            "additions": original.additions,
            "deletions": original.deletions,
        })
    safe_changes = {
        "ok": True,
        "files": safe_files,
        "diff": redacted_diff,
        "truncated": diff_truncated,
        "changed_count": total,
        "mode": str(source.get("mode") or ""),
        "root": str(source.get("root") or ""),
    }
    prompt = render_review_prompt(
        project=project,
        task=safe_task,
        writer_summary=safe_summary,
        changes=safe_changes,
        recent_log=safe_log,
        change_brief=safe_brief,
        project_map=safe_map,
        verification_map=safe_verification,
        review_impact_map=safe_impact,
        execution_evidence=safe_evidence,
    )
    scope = ReviewScope(
        total_changed_files=total,
        provided_files=provided,
        excluded_files=tuple(excluded),
        diff_truncated=diff_truncated,
        file_list_truncated=file_list_truncated,
        context_truncated=context_truncated,
        collection_incomplete=source.get("changed_count", total) != total,
        required_context_truncated=len(safe_task) > 6000 or len(safe_brief) > 8000,
        exclusion_reasons=tuple(reasons[:20]),
        content_redacted=any(original != safe for original, safe in (
            (filtered_diff, redacted_diff), (task, safe_task), (writer_summary, safe_summary),
            (recent_log, safe_log), (change_brief, safe_brief), (project_map, safe_map),
            (verification_map, safe_verification), (review_impact_map, safe_impact),
            (execution_evidence, safe_evidence),
        )),
    )
    return ReviewInput(
        change_set=change_set,
        reviewer_view=safe_changes,
        prompt=prompt,
        scope=scope,
    )


def _sensitive_file_reason(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip("/")
    if not normalized:
        return ""
    parts = [p for p in normalized.split("/") if p]
    for part in parts:
        lower = part.lower()
        if lower in _SENSITIVE_FILENAMES:
            return "sensitive_filename"
        if any(lower.endswith(s) for s in _SENSITIVE_SUFFIXES):
            return "sensitive_suffix"
        # Module names such as private_helpers.py and credentials_parser.py
        # are ordinary code. Only dedicated secret directories are excluded.
        if part != parts[-1] and lower in _SENSITIVE_NAME_PARTS - {"auth", "private"}:
            return "secret_like_path"
    return ""


def _redact_text(text: object) -> str:
    from codey.policies.redaction import looks_high_entropy_secret, looks_secret_shape

    raw = "" if text is None else str(text)
    if not raw:
        return raw
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    redacted: list[str] = []
    in_private_key = False
    for line in lines:
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            in_private_key = True
        if in_private_key:
            marker = line[:1] if line[:1] in {"+", "-", " "} else ""
            redacted.append(marker + _REDACTED)
            if "-----END " in line and "PRIVATE KEY-----" in line:
                in_private_key = False
        elif looks_secret_shape(line) or looks_high_entropy_secret(line):
            redacted.append(_redact_line(line))
        else:
            redacted.append(line)
    return "\n".join(redacted)


def _redact_line(line: str) -> str:
    import re

    text = re.sub(r"sk-[A-Za-z0-9_\-]{16,}", _REDACTED, line)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_]{20,}", _REDACTED, text)
    text = re.sub(r"xox[baprs]-[A-Za-z0-9\-]{16,}", _REDACTED, text)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", _REDACTED, text)
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n]*", _REDACTED, text)
    text = re.sub(r"\b[A-Za-z0-9][A-Za-z0-9_\-./+=]{16,}\b", _redact_token, text)
    return text


def _redact_token(match: re.Match[str]) -> str:
    import re

    from codey.policies.redaction import SECRET_MARKER_RE
    from codey.policies.redaction import looks_high_entropy_secret as _looks

    token = match.group(0)
    # Keep the surrounding marker: lowercase hex and slash-bearing secrets
    # only qualify when the caller supplied their credential context.
    prefix = match.string[:match.start()]
    markers = list(SECRET_MARKER_RE.finditer(prefix))
    credential_value = bool(markers and re.fullmatch(r'''[\s:=?"']*''', prefix[markers[-1].end():]))
    if _looks(token) or credential_value and _looks("api_key=" + token):
        return _REDACTED
    return token


def _filter_diff_to_provided(diff: str, provided: set[str]) -> tuple[str, bool]:
    if not diff:
        return diff, False
    if not provided:
        return "", False
    from codey.workspace.change_set import _path_from_diff_git as _diff_path
    from codey.workspace.change_set import _path_from_file_header as _header_path

    lines = diff.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    current: list[str] = []
    current_paths: set[str] = set()
    current_has_hunk = False
    incomplete = False

    def _flush() -> None:
        nonlocal incomplete
        if not current:
            return
        keep = bool(current_paths) and current_paths.issubset(provided)
        if not current_paths:
            incomplete = True
        if keep:
            kept.extend(current)

    for line in lines:
        if line.startswith("diff --git "):
            _flush()
            current = [line]
            current_paths = set()
            current_has_hunk = False
            diff_path = _diff_path(line)
            if diff_path:
                current_paths.add(diff_path)
            continue
        if (
            line.startswith("--- ")
            and current
            and any(item.startswith("+++") for item in current)
            and not current_has_hunk
        ):
            _flush()
            current = [line]
            current_paths = set()
            current_has_hunk = False
            header_path = _header_path(line[4:])
            if header_path:
                current_paths.add(header_path)
            continue
        if not current:
            if line.startswith("--- "):
                current = [line]
                current_paths = set()
                current_has_hunk = False
                header_path = _header_path(line[4:])
                if header_path:
                    current_paths.add(header_path)
            else:
                kept.append(line)
            continue
        current.append(line)
        if line.startswith("@@"):
            current_has_hunk = True
            continue
        if line.startswith("--- ") and not current_has_hunk:
            header_path = _header_path(line[4:])
            if header_path:
                current_paths.add(header_path)
            continue
        if line.startswith("+++ ") and not current_has_hunk:
            header_path = _header_path(line[4:])
            if header_path:
                current_paths.add(header_path)
            continue
    _flush()
    return "\n".join(kept), incomplete
