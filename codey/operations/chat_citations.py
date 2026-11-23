"""Resolve explicit citations from the existing committed source views."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import quote

from codey.utils.citation_scanner import mask_markdown_code

_REFERENCE = re.compile(r"(?<![!\[])\[([sr][0-9][^\]\n]*)\]")


def _source_link(label: str, url: str) -> str:
    destination = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
    return f"[{label}](<{destination}>)"


def render_source_citations(text: str, *, sources: Mapping[str, str], searches: Mapping[str, str],
                            literal: bool = False, required_sources: tuple[str, ...] = ()) -> str:
    """Render once before the existing completion result is persisted.

    Citation syntax is reserved in prose. JSON and Markdown code are literal.
    A caller may supply explicitly required sources from committed receipts.
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
            links.append(_source_link(label, url))
        parts.extend((text[offset:match.start()], "".join(links)))
        offset = match.end()
    rendered = "".join(parts) + text[offset:]
    if required_sources:
        prose = mask_markdown_code(rendered)
        missing = [link for label in required_sources
                   if (url := sources[label]) not in prose and (link := _source_link(label, url)) not in prose]
        if missing:
            return rendered.rstrip() + "\n\n## Sources\n" + "\n".join("- " + link for link in missing)
    return rendered
