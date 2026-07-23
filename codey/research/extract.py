"""Turn raw HTML into clean readable text."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "li", "ul", "ol",
    "table", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r"[ \t\f\v]+")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _HEADING_TAGS:
            self.parts.append("\n\n## ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth > 0:
            return
        text = data.strip("\n")
        if text.strip():
            self.parts.append(text)


def extract_text(html: str) -> str:
    parser = _Extractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    text = "".join(parser.parts)
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line is not None)
    return _MULTI_BLANK_RE.sub("\n\n", text).strip()


def extract_title(html: str) -> str:
    parser = _Extractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return _SPACES_RE.sub(" ", parser.title).strip()
