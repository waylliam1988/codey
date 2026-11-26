from __future__ import annotations

import re

from codey.research.answer_values import CitationOccurrence
from codey.utils.citation_scanner import mask_markdown_code


def citation_occurrences(markdown: str) -> list[CitationOccurrence]:
    """Record explicit prose citations; code examples do not select evidence."""
    masked = mask_markdown_code(markdown)
    rows: list[CitationOccurrence] = []
    for match in re.finditer(r"(?<!\\)\[(e[0-9][^\]\n]*)\](?!\()", masked):
        labels = re.split(r"\s*[,，]\s*", match[1])
        if any(re.fullmatch(r"e[1-9][0-9]*", label) is None for label in labels):
            raise ValueError("Use explicit evidence labels such as [e1][e2] or [e1, e2]; no ranges or invented labels.")
        unique_labels: dict[str, None] = dict.fromkeys(labels)
        rows.extend({"label": label, "start": match.start(), "end": match.end()} for label in unique_labels)
    return rows
