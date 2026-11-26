"""File creation and version-checked text editing with contextual diagnostics."""

from __future__ import annotations

import ast
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from codey.toolchain.constants import (
    EXACT_REPLACEMENT_CONTEXT_HINT,
    MAX_REPLACEMENTS,
    STALE_READ_REFERENCE_CODE,
    STALE_READ_REFERENCE_ERROR,
)
from codey.toolchain.edit_text import TextEdit, count_matches, exact_raw_edit, normalized_text, trailing_space_matches
from codey.toolchain.indentation import align_replacement, boundary_replacement, uniform_replacement, valid_python
from codey.toolchain.line_prefix import strip_line_number_prefixes as _strip_line_number_prefixes
from codey.toolchain.outcome import ToolOutcome
from codey.toolchain.path_checks import _checked_tool_path, _verify_file_for_use, _write_text_after_final_path_check
from codey.toolchain.presentation import edit_preview
from codey.types.json_value import JsonObject, JsonValue

WRITE_MAX_FILE_BYTES = 512 * 1024
EDIT_FAILURE_MAX_CHARS = 1_600
EDIT_FAILURE_MAX_LINES = 7
EDIT_FAILURE_MAX_MATCHES = 3
EDIT_FAILURE_MAX_LINE_CHARS = 400
EDIT_FAILURE_MAX_ANCHOR_CANDIDATES = 32
PYTHON_SYNTAX_HINT_MAX_CHARS = 128 * 1024
PYTHON_SYNTAX_HINT_MAX_MESSAGE_CHARS = 160


def retry_replacement_without_line_numbers(content: str, block: EditBlock) -> EditBlock | None:
    stripped_search, changed_search = _strip_line_number_prefixes(block.old_string)
    stripped_replace, changed_replace = _strip_line_number_prefixes(block.new_string)
    if not changed_search and not changed_replace:
        return None
    candidate = EditBlock(old_string=stripped_search, new_string=stripped_replace if changed_replace else block.new_string)
    if not candidate.old_string:
        return None
    # Stripped search must match exactly once, otherwise refuse (atomicity).
    if count_matches(content, candidate.old_string) != 1:
        return None
    # Never write line numbers into the file: the replacement must not start
    # with a stripped prefix shape that we just removed.
    return candidate


EDIT_ANCHOR_RE = re.compile(
    r"(?P<quote>['\"])(?P<literal>[^'\"\r\n]{4,})(?P=quote)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]{3,})"
)
EDIT_ANCHOR_STOPWORDS = {
    "class",
    "const",
    "else",
    "false",
    "function",
    "import",
    "return",
    "true",
}


@dataclass(frozen=True)
class EditBlock:
    old_string: str
    new_string: str


def replacement_blocks(replacements: JsonValue) -> list[EditBlock]:
    if not isinstance(replacements, list) or not replacements:
        raise ValueError("edit needs content or exact replacements")
    blocks: list[EditBlock] = []
    for item in replacements:
        if not isinstance(item, dict) or set(item) != {"old_string", "new_string"}:
            raise ValueError("edit replacement must be exactly old_string/new_string")
        old, new = item["old_string"], item["new_string"]
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("edit replacement requires text and a non-empty search")
        blocks.append(EditBlock(old, new))
    return blocks


def write_file(root: Path, rel: str, content: str, *, expected_version: str | None = None) -> ToolOutcome:
    if len(content.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return ToolOutcome.error(f"file too large to write: {rel}")
    path, error = _checked_tool_path(root, rel, tool="write_file")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    before: str | None = ""
    try:
        if path.is_file():
            try:
                before = path.read_text(encoding="utf-8")
                if before == content:
                    if expected_version is not None:
                        from codey.workspace.paths import path_hash

                        raw = expected_version.startswith("raw:")
                        if path_hash(path, preserve_newlines=raw) != expected_version.removeprefix("raw:"):
                            return ToolOutcome.error(STALE_READ_REFERENCE_ERROR, error_code=STALE_READ_REFERENCE_CODE)
                    return ToolOutcome(f"wrote {rel} (no changes)", True)
            except (OSError, UnicodeDecodeError, ValueError):
                # Best-effort hint only: the authoritative path check
                # runs again inside _write_text_after_final_path_check.
                before = None
    except OSError:
        before = None
    write_error = _write_text_after_final_path_check(
        root,
        rel,
        content,
        tool="write_file",
        expected_version=expected_version,
    )
    if write_error is not None:
        return write_error
    syntax_hint = _python_syntax_regression_hint(rel, before or "", content)
    return ToolOutcome(
        f"wrote {rel} ({len(content)} chars){syntax_hint}",
        True,
        changed=True,
        presentation={"preview": edit_preview(rel, before, content)},
    )


def _bounded_failure_output(lines: list[str]) -> str:
    max_chars = EDIT_FAILURE_MAX_CHARS - len("ERROR: ")
    rendered: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if rendered else 0)
        if size + extra > max_chars:
            note = "Additional failure context omitted by character budget."
            note_extra = len(note) + (1 if rendered else 0)
            if size + note_extra <= max_chars:
                rendered.append(note)
            break
        rendered.append(line)
        size += extra
    return "\n".join(rendered)


def _unique_anchor_position(content: str, value: str, kind: str) -> int | None:
    if kind == "literal":
        first = content.find(value)
        if first < 0 or content.find(value, first + 1) >= 0:
            return None
        return first
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])"
    )
    matches = pattern.finditer(content)
    first_match = next(matches, None)
    if first_match is None or next(matches, None) is not None:
        return None
    return first_match.start()


def _edit_anchor(content: str, search: str) -> tuple[str, str, int] | None:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in EDIT_ANCHOR_RE.finditer(search):
        literal: str | None = match.group("literal")
        identifier: str | None = match.group("identifier")
        value = literal or identifier or ""
        if (
            not value
            or len(value) > EDIT_FAILURE_MAX_LINE_CHARS
            or value in seen
            or value.lower() in EDIT_ANCHOR_STOPWORDS
        ):
            continue
        seen.add(value)
        kind = "literal" if literal is not None else "identifier"
        candidates.append((value, kind))
    def descending_length(item: tuple[str, str]) -> int:
        return -len(item[0])

    candidates.sort(key=descending_length)
    for value, kind in candidates[:EDIT_FAILURE_MAX_ANCHOR_CANDIDATES]:
        position = _unique_anchor_position(content, value, kind)
        if position is not None:
            return value, kind, position
    return None


def _line_vote_positions(lines: list[str], search_lines: list[str]) -> tuple[int, ...]:
    offsets: dict[str, list[int]] = {}
    for index, line in enumerate(search_lines):
        key = unicodedata.normalize("NFC", line.strip())
        if any(char.isalnum() for char in key):
            offsets.setdefault(key, []).append(index)
    votes: Counter[int] = Counter()
    for index, line in enumerate(lines):
        key = unicodedata.normalize("NFC", line.strip())
        for offset in offsets.get(key, ()):
            if index >= offset:
                votes[index - offset] += 1
    best = max(votes.values(), default=0)
    if best < 2:
        return ()
    return tuple(sorted(position for position, count in votes.items() if count == best)[:EDIT_FAILURE_MAX_MATCHES])


def _render_edit_failure_context(content: str, search: str) -> str:
    anchor = _edit_anchor(content, search)
    lines = content.splitlines()
    search_lines = search.splitlines()
    if anchor is None:
        positions = _line_vote_positions(lines, search_lines)
        if not positions:
            return ""
        rendered = ["Candidate replacement starts at lines: " + ", ".join(str(index + 1) for index in positions) + ".",
                    EXACT_REPLACEMENT_CONTEXT_HINT]
        for start in positions:
            rendered.append(f"Context for candidate at line {start + 1}:")
            rendered.extend(_render_edit_context_lines(lines, start, min(start + EDIT_FAILURE_MAX_LINES, len(lines))))
        return _bounded_failure_output(rendered)
    value, kind, position = anchor
    center = content.count("\n", 0, position)
    radius = EDIT_FAILURE_MAX_LINES // 2
    start = max(0, center - radius)
    end = min(len(lines), start + EDIT_FAILURE_MAX_LINES)
    start = max(0, end - EDIT_FAILURE_MAX_LINES)
    rendered = [
        f'Current bounded context near {kind} "{value}"',
        '(navigation only; copy exact complete code after "|"):',
    ]
    search_position = _unique_anchor_position(search, value, kind)
    if search_position is not None:
        search_center = search.count("\n", 0, search_position)
        excerpt_start = max(0, search_center - radius)
        requested = search_lines[excerpt_start:excerpt_start + EDIT_FAILURE_MAX_LINES]
        candidate_start = max(0, center - (search_center - excerpt_start))
        candidate = lines[candidate_start:candidate_start + len(requested) + 2]
        for tag, old_start, old_end, new_start, new_end in SequenceMatcher(
                None, requested, candidate, autojunk=False).get_opcodes():
            if tag != "equal" and old_start < len(requested):
                rendered.extend([
                    "Requested old text: " + repr("\n".join(requested[old_start:old_end]))[:240],
                    "Current file near anchor: " + repr("\n".join(candidate[new_start:new_end]))[:240],
                ])
                break
    rendered.extend(_render_edit_context_lines(lines, start, end))
    return _bounded_failure_output(rendered)


def _render_edit_context_lines(lines: list[str], start: int, end: int) -> list[str]:
    rendered: list[str] = []
    for index in range(start, end):
        line = lines[index]
        if len(line) > EDIT_FAILURE_MAX_LINE_CHARS:
            rendered.append(
                f"Line {index + 1} omitted because it exceeds "
                f"{EDIT_FAILURE_MAX_LINE_CHARS} characters; use read_file "
                f"offset={index + 1} limit=1."
            )
            continue
        rendered.append(f"{index + 1:>4} | {line}")
    return rendered


def _first_match_start_lines(
    content: str,
    search: str,
    limit: int = EDIT_FAILURE_MAX_MATCHES,
) -> list[int]:
    if not search:
        return []
    lines: list[int] = []
    offset = 0
    newline_offset = 0
    line_number = 1
    while len(lines) < limit:
        position = content.find(search, offset)
        if position < 0:
            break
        line_number += content.count("\n", newline_offset, position)
        lines.append(line_number)
        newline_offset = position
        offset = position + 1
    return lines


def _render_multiple_match_context(
    content: str,
    match_count: int,
    positions: list[int],
    *,
    exact: bool = True,
) -> str:
    if not positions:
        return ""
    label = "Exact matches" if exact else "Matches after ignoring line-end space"
    lines = [label + " start at lines: " + ", ".join(map(str, positions)) + "."]
    if match_count > len(positions):
        lines.append("Additional matches omitted.")
    lines.append(EXACT_REPLACEMENT_CONTEXT_HINT)
    source_lines = content.split("\n")
    for position in positions:
        lines.append(f"Context for match at line {position}:")
        for index in range(max(0, position - 3), position):
            line = source_lines[index]
            if len(line) > 80:
                lines.append(f"Line {index + 1} omitted; use read_file offset={index + 1} limit=1.")
            else:
                lines.append(f"{index + 1:>4} | {line}")
    return _bounded_failure_output(lines)


def _replacement_failure_note(index: int, count: int) -> str:
    if count <= 1:
        return ""
    return f"Replacement {index} of {count} failed. No replacements were written."


def _search_not_found(
    rel: str,
    *,
    original_content: str,
    search: str,
    replacement: str,
    replacement_index: int,
    replacement_count: int,
) -> ToolOutcome:
    lines = [f"SEARCH text not found in {rel}; exact replacement was not applied."]
    note = _replacement_failure_note(replacement_index, replacement_count)
    if note:
        lines.append(note)
    if len(replacement.strip()) >= 20 and replacement in original_content:
        lines.append("new_string is already in the file, so this edit may already be applied. "
                     "This is only a hint; no replacement was written. Verify its location before retrying.")
    context = _render_edit_failure_context(original_content, search)
    if context:
        lines.append("")
        lines.extend(context.splitlines())
    else:
        lines.extend(["Use read_file to inspect the current file text.", EXACT_REPLACEMENT_CONTEXT_HINT])
    positions = _line_vote_positions(original_content.splitlines(), search.splitlines())
    return ToolOutcome.error(_bounded_failure_output(lines), error_code="edit_search_not_found",
                             audit={"search_candidate_lines": [position + 1 for position in positions],
                                    "replacement_index": replacement_index})


def _multiple_matches(
    rel: str,
    match_count: int,
    *,
    original_content: str,
    search: str,
    replacement_index: int,
    replacement_count: int,
) -> ToolOutcome:
    lines = [
        f"SEARCH text matched {match_count} times in {rel}; replacement was not applied."
    ]
    note = _replacement_failure_note(replacement_index, replacement_count)
    if note:
        lines.append(note)
    original_count = count_matches(original_content, search)
    context = (
        _render_multiple_match_context(original_content, match_count, _first_match_start_lines(original_content, search))
        if original_count == match_count
        else ""
    )
    if context:
        lines.append("")
        lines.extend(context.splitlines())
    else:
        lines.append(EXACT_REPLACEMENT_CONTEXT_HINT)
    return ToolOutcome.error(_bounded_failure_output(lines))


def _python_syntax_regression_hint(rel: str, before: str, after: str) -> str:
    if Path(rel).suffix.lower() != ".py":
        return ""
    if max(len(before), len(after)) > PYTHON_SYNTAX_HINT_MAX_CHARS:
        return ""

    parse_failures = (
        SyntaxError,
        ValueError,
        TypeError,
        MemoryError,
        RecursionError,
        OverflowError,
    )
    try:
        ast.parse(before, filename=rel)
    except parse_failures:
        return ""

    try:
        ast.parse(after, filename=rel)
    except SyntaxError as exc:
        message = " ".join((exc.msg or "invalid syntax").split())
        message = message[:PYTHON_SYNTAX_HINT_MAX_MESSAGE_CHARS]
        location = f"line {max(1, (exc.lineno or 1))}"
        if exc.offset:
            location += f", column {max(1, exc.offset)}"
        return (
            f"\nSyntax regression detected in {rel} at {location}: {message}.\n"
            "The edit was applied. Inspect and repair the current file "
            "before completion."
        )
    except (ValueError, TypeError, MemoryError, RecursionError, OverflowError):
        return ""

    return ""


def _line_end_space_edit(rel: str, view: str, old: str, new: str, index: int, count: int) -> TextEdit | ToolOutcome | None:
    matches = trailing_space_matches(view, old)
    if len(matches) > 1:
        positions = [view.count("\n", 0, start) + 1 for start, _ in matches[:EDIT_FAILURE_MAX_MATCHES]]
        return ToolOutcome.error(_bounded_failure_output([
            f"SEARCH text matched {len(matches)} times in {rel} after ignoring line-end space; replacement was not applied.",
            *_render_multiple_match_context(view, len(matches), positions, exact=False).splitlines(),
            _replacement_failure_note(index, count),
        ]))
    if matches:
        start, end = matches[0]
        return TextEdit(start, end, new)
    return None


def _edit_block_error(blocks: list[EditBlock]) -> str:
    if not blocks:
        return "edit requires at least one replacement"
    if len(blocks) > MAX_REPLACEMENTS:
        return f"edit supports at most {MAX_REPLACEMENTS} replacements"
    if any(not block.old_string for block in blocks):
        return "edit SEARCH text cannot be empty"
    return ""


@dataclass(frozen=True)
class PreparedEdit:
    text: str
    version: str
    outcome: ToolOutcome
    syntax_regression: bool = False


def _recover_indentation(rel: str, view: str, old: str, new: str, index: int, count: int) -> TextEdit | ToolOutcome | None:
    from codey.toolchain.ecmascript import ASSISTED_SUFFIXES, ecmascript_language, safe_alignment

    suffix = Path(rel).suffix.lower()
    if suffix == ".py":
        return align_replacement(view, old, new)
    if suffix not in ASSISTED_SUFFIXES or len(view.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return None
    language = ecmascript_language(suffix)
    if language is None:
        return None
    candidate, ambiguous = uniform_replacement(view, old, new)
    if ambiguous:
        locations = ", ".join(map(str, ambiguous))
        return ToolOutcome.error(_bounded_failure_output([
            f"SEARCH text matches multiple indentation-aligned locations in {rel}.",
            f"Candidate start lines in the current draft include {locations}.",
            EXACT_REPLACEMENT_CONTEXT_HINT,
            _replacement_failure_note(index, count),
        ]))
    return candidate if candidate is not None and safe_alignment(language, view, candidate) else None


def _valid_assisted_syntax(suffix: str, text: str) -> bool:
    from codey.toolchain.ecmascript import ecmascript_language, valid_ecmascript

    if suffix == ".py":
        return valid_python(normalized_text(text))
    language = ecmascript_language(suffix)
    return language is not None and valid_ecmascript(language, normalized_text(text))


def _read_edit_target(root: Path, rel: str) -> str | ToolOutcome:
    path, error = _checked_tool_path(root, rel, tool="edit")
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    verify_error = _verify_file_for_use(root, rel, path, tool="edit")
    if verify_error is not None:
        return verify_error
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError:
        return ToolOutcome.error(f"not utf-8 text: {rel}")
    except (OSError, ValueError):
        return ToolOutcome.error(f"cannot read file: {rel}")


def prepare_edit(root: Path, rel: str, blocks: list[EditBlock], *,
                 expected_version: str = "",
                 transform_content: Callable[[str, str], tuple[str, JsonObject]] | None = None) -> PreparedEdit | ToolOutcome:
    if problem := _edit_block_error(blocks):
        return ToolOutcome.error(problem)
    content = _read_edit_target(root, rel)
    if isinstance(content, ToolOutcome):
        return content
    from codey.workspace.paths import content_hash

    version = content_hash(content)
    if expected_version and version != expected_version:
        return ToolOutcome.error(STALE_READ_REFERENCE_ERROR, error_code=STALE_READ_REFERENCE_CODE)

    updated = content
    suffix = Path(rel).suffix.lower()
    aligned = 0
    boundary_adjusted = 0
    whitespace_adjusted = 0
    for index, block in enumerate(blocks, start=1):
        view = normalized_text(updated)
        old, new = normalized_text(block.old_string), normalized_text(block.new_string)
        raw_edit = exact_raw_edit(updated, view, block.old_string, new)
        if raw_edit is not None:
            updated = raw_edit.apply(updated)
            continue
        total = count_matches(view, old)
        if total == 0:
            spacing = _line_end_space_edit(rel, view, old, new, index, len(blocks))
            if isinstance(spacing, ToolOutcome):
                return spacing
            if spacing is not None:
                updated = spacing.apply(updated)
                whitespace_adjusted += 1
                continue
            retried = retry_replacement_without_line_numbers(view, EditBlock(old, new))
            if retried is not None:
                start = view.find(retried.old_string)
                updated = TextEdit(start, start + len(retried.old_string), retried.new_string).apply(updated)
                continue
            recovered = _recover_indentation(rel, view, old, new, index, len(blocks))
            if isinstance(recovered, ToolOutcome):
                return recovered
            if recovered is not None:
                updated = recovered.apply(updated)
                aligned += 1
                continue
            boundary = boundary_replacement(view, old, new) if suffix == ".py" else None
            if boundary is not None:
                updated = boundary.apply(updated)
                boundary_adjusted += 1
                continue
            return _search_not_found(
                rel,
                original_content=normalized_text(content),
                search=old,
                replacement=new,
                replacement_index=index,
                replacement_count=len(blocks),
            )
        if total > 1:
            return _multiple_matches(
                rel,
                total,
                original_content=normalized_text(content),
                search=old,
                replacement_index=index,
                replacement_count=len(blocks),
            )
        start = view.find(old)
        updated = TextEdit(start, start + len(old), new).apply(updated)

    if updated == content:
        return PreparedEdit(updated, version, ToolOutcome(f"edited {rel} (no changes)", True))
    assistance: JsonObject = {}
    if transform_content is not None:
        updated, assistance = transform_content(content, updated)
    if len(updated.encode("utf-8")) > WRITE_MAX_FILE_BYTES:
        return ToolOutcome.error(f"file too large to write: {rel}")
    if (aligned or boundary_adjusted or (whitespace_adjusted and suffix == ".py")) and not _valid_assisted_syntax(suffix, updated):
        language_name = "Python" if suffix == ".py" else "JS/TS"
        return ToolOutcome.error(f"automatic edit assistance cannot safely apply this batch: resulting {language_name} is invalid; no replacements written")
    syntax_hint = _python_syntax_regression_hint(rel, content, updated)
    count = len(blocks)
    label = "replacement" if count == 1 else "replacements"
    assistance_audit: dict[str, object] = {
        key: value for key, value in (("indentation_adjusted", aligned), ("boundary_adjusted", boundary_adjusted),
                                      ("line_end_space_adjusted", whitespace_adjusted)) if value
    }
    outcome = ToolOutcome(
        f"edited {rel} ({count} {label})"
        + (f"\nAutomatically aligned outer indentation for {aligned} replacement(s)." if aligned else "")
        + (f"\nAutomatically adjusted fragment boundary for {boundary_adjusted} replacement(s)." if boundary_adjusted else "")
        + (f"\nMatched {whitespace_adjusted} replacement(s) after ignoring line-end space." if whitespace_adjusted else "")
        + syntax_hint,
        True,
        changed=True,
        audit={**assistance_audit, **assistance},
        presentation={"preview": edit_preview(rel, content, updated)},
    )
    return PreparedEdit(updated, version, outcome, syntax_regression=bool(syntax_hint))


def edit_file(root: Path, rel: str, blocks: list[EditBlock]) -> ToolOutcome:
    return edit_file_at_version(root, rel, blocks)


def edit_file_at_version(root: Path, rel: str, blocks: list[EditBlock], *, version: str = "",
                         validate_content: Callable[[str], str] | None = None,
                         transform_content: Callable[[str, str], tuple[str, JsonObject]] | None = None) -> ToolOutcome:
    prepared = prepare_edit(root, rel, blocks, expected_version=version, transform_content=transform_content)
    if isinstance(prepared, ToolOutcome):
        return prepared
    if validate_content is not None and (validation_error := validate_content(prepared.text)):
        return ToolOutcome.error(validation_error, error_code="file_field_mismatch")
    if not prepared.outcome.changed:
        return prepared.outcome
    error = _write_text_after_final_path_check(root, rel, prepared.text, tool="edit", expected_version=prepared.version)
    return error if error is not None else prepared.outcome
