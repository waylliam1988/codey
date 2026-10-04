"""Safe review input preparation with explicit scope."""
from __future__ import annotations

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
    exclusion_reasons: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not (
            self.collection_incomplete
            or self.diff_truncated
            or self.file_list_truncated
            or bool(self.excluded_files)
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
    for item in all_files:
        reason = _sensitive_file_reason(item.path)
        if reason:
            excluded.append(item.path)
            reasons.append(f"{item.path}:{reason}")
        else:
            kept.append(item.path)
    file_list_truncated = len(kept) > 20
    provided = tuple(kept[:20])
    raw_diff = str(source.get("diff") or "")
    upstream_truncated = bool(source.get("truncated"))
    filtered_diff = _filter_diff_to_provided(raw_diff, set(provided))
    redacted_diff = _redact_text(filtered_diff)
    diff_truncated = upstream_truncated or len(redacted_diff) > MAX_REVIEW_DIFF_CHARS
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
        collection_incomplete=False,
        exclusion_reasons=tuple(reasons[:20]),
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
        if lower in _SENSITIVE_NAME_PARTS or any(m in lower for m in ("secret", "credential", "private")):
            if lower in {"token", "tokens", "password", "passwd"}:
                continue
            return "secret_like_path"
    return ""


def _redact_text(text: object) -> str:
    from codey.policies.redaction import looks_high_entropy_secret, looks_secret_shape

    raw = "" if text is None else str(text)
    if not raw:
        return raw
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    redacted: list[str] = []
    for line in lines:
        if looks_secret_shape(line) or looks_high_entropy_secret(line):
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
    if text == line:
        return _REDACTED if len(line) > 200 else line
    return text


def _redact_token(match: object) -> str:
    from codey.policies.redaction import looks_high_entropy_secret as _looks

    token = match.group(0) if hasattr(match, "group") else str(match)
    if _looks(token):
        return _REDACTED
    return token


def _filter_diff_to_provided(diff: str, provided: set[str]) -> str:
    if not diff:
        return diff
    if not provided:
        return ""
    from codey.workspace.change_set import _path_from_diff_git as _diff_path
    from codey.workspace.change_set import _path_from_file_header as _header_path

    lines = diff.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    current: list[str] = []
    current_path = ""
    pending_old = ""
    pending_new = ""

    def _flush() -> None:
        if not current:
            return
        keep = not current_path or current_path in provided
        if keep:
            kept.extend(current)

    for line in lines:
        if line.startswith("diff --git "):
            _flush()
            current = [line]
            current_path = _diff_path(line)
            pending_old = ""
            pending_new = ""
            continue
        if not current:
            kept.append(line)
            continue
        current.append(line)
        if line.startswith("--- "):
            pending_old = _header_path(line[4:])
            continue
        if line.startswith("+++ "):
            pending_new = _header_path(line[4:])
            selected = pending_new or pending_old or current_path
            if selected:
                current_path = selected
            continue
    _flush()
    return "\n".join(kept)
