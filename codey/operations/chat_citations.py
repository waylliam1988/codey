"""Resolve explicit Chat citations from the existing committed source views."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import quote

from codey.utils.citation_scanner import mask_markdown_code

_REFERENCE = re.compile(r"(?<![!\[])\[([sr][0-9][^\]\n]*)\]")


def render_source_citations(text: str, *, sources: Mapping[str, str], searches: Mapping[str, str],
                            literal: bool = False) -> str:
    """Render once before the existing completion result is persisted.

    Citation syntax is reserved in prose. JSON and Markdown code are literal;
    no inferred references, URL correction, extra sources or footer is added.
    """
    if literal:
        return text
    try:
        json.loads(text)
    except ValueError:
        pass
    else:
        return text
    masked = mask_markdown_code(text)
    parts: list[str] = []
    offset = 0
    for match in _REFERENCE.finditer(masked):
        preceding = text[:match.start()]
        if (len(preceding) - len(preceding.rstrip("\\"))) % 2:
            continue
        labels = re.split(r"\s*[,，]\s*", match[1])
        if any(re.fullmatch(r"[sr][1-9][0-9]*", label) is None for label in labels):
            raise ValueError("Use explicit source labels such as [s1][s2] or [r1]; no ranges or invented labels.")
        suffix = text[match.end():]
        if (suffix.startswith("(")
                or suffix.startswith("[") and _REFERENCE.match(masked, match.end()) is None
                or suffix.startswith(":") and not preceding.rsplit("\n", 1)[-1].strip()
                or preceding.endswith("]") and offset != match.start()):
            raise ValueError("Use bare source labels such as [s1]; the application supplies link destinations.")
        links: list[str] = []
        for label in dict.fromkeys(labels):
            url = (sources if label.startswith("s") else searches).get(label)
            if url is None:
                raise ValueError(f"Unknown source label [{label}]. Available read sources: "
                    + (", ".join(sources) or "none") + "; search links: " + (", ".join(searches) or "none")
                    + ". Reuse acquired results and select only sources used in the answer; do not invent URLs.")
            destination = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
            links.append(f"[{label}](<{destination}>)")
        parts.extend((text[offset:match.start()], "".join(links)))
        offset = match.end()
    return "".join(parts) + text[offset:]
