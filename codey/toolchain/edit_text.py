"""Apply LF-view edit coordinates without rewriting untouched source bytes."""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from difflib import SequenceMatcher

_NEWLINE = re.compile(r"\r\n|\r|\n")


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
