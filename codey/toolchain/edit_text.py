"""Apply LF-view edit coordinates without rewriting untouched source bytes."""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from difflib import SequenceMatcher

_NEWLINE = re.compile(r"\r\n|\r|\n")
_LINE_TAIL = re.compile(r"[ \t]+(?=\n)")


def normalized_text(source: str) -> str:
    """Use the same universal-newline view as read_file and file-version hashes."""
    return source.replace("\r\n", "\n").replace("\r", "\n")


def count_matches(content: str, search: str) -> int:
    """Count overlapping occurrences too; they cannot authorize a unique edit."""
    count, offset = 0, 0
    while (position := content.find(search, offset)) >= 0:
        count += 1
        offset = position + 1
    return count


def trailing_space_matches(content: str, search: str) -> list[tuple[int, int]]:
    """Find LF-coordinate spans while ignoring horizontal space before line breaks."""
    if "\n" not in search:
        return []
    needle = _LINE_TAIL.sub("", search)
    if not needle.strip():
        return []
    removed: list[int] = []
    shifts: list[int] = []
    shift = 0
    for match in _LINE_TAIL.finditer(content):
        removed.append(match.start() - shift)
        shift += match.end() - match.start()
        shifts.append(shift)
    view = _LINE_TAIL.sub("", content)

    def source_index(index: int) -> int:
        skipped = bisect_left(removed, index + 1)
        return index + (shifts[skipped - 1] if skipped else 0)

    matches: list[tuple[int, int]] = []
    offset = 0
    while (position := view.find(needle, offset)) >= 0:
        span = source_index(position), source_index(position + len(needle) - 1) + 1
        matches.append(span)
        offset = position + 1
    return matches


def exact_raw_edit(source: str, view: str, search: str, replacement: str) -> TextEdit | None:
    if source == view or count_matches(source, search) != 1:
        return None
    start = source.find(search)
    end = start + len(search)
    if any(offset > 0 and source[offset - 1:offset + 1] == "\r\n" for offset in (start, end)):
        return None
    return TextEdit(len(normalized_text(source[:start])), len(normalized_text(source[:end])), replacement)


def _region_endings(region: str, replacement: str, fallback: str) -> str:
    before, after = normalized_text(region).split("\n"), replacement.split("\n")
    endings = [match[0] for match in _NEWLINE.finditer(region)]
    inherited: dict[int, str] = {}
    for tag, old_start, old_end, new_start, new_end in SequenceMatcher(None, before, after).get_opcodes():
        if tag in {"equal", "replace"}:
            for old, new in zip(range(old_start, old_end), range(new_start, new_end), strict=False):
                if old < len(endings):
                    inherited[new] = endings[old]
    return "".join(line + (inherited.get(index, fallback) if index < len(after) - 1 else "")
                   for index, line in enumerate(after))


@dataclass(frozen=True)
class TextEdit:
    """One replacement in the normalized text's character coordinates."""
    start: int
    end: int
    replacement: str

    def apply(self, source: str) -> str:
        removed = [match.start() - index for index, match in enumerate(re.finditer("\r\n", source))]
        start = self.start + bisect_left(removed, self.start)
        end = self.end + bisect_left(removed, self.end)
        nearby = _NEWLINE.search(source, start)
        fallback = nearby[0] if nearby is not None else next(
            (match[0] for match in reversed(list(_NEWLINE.finditer(source, 0, start)))), "\n",
        )
        replacement = _region_endings(source[start:end], normalized_text(self.replacement), fallback)
        return source[:start] + replacement + source[end:]
